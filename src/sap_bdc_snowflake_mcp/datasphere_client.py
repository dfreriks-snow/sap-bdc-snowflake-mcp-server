"""Read-only SAP Datasphere OData client.

Ported (read paths only) from Mario DeFelipe's sap-datasphere-mcp
(https://github.com/MarioDeFelipe/sap-datasphere-mcp). Talks directly to the SAP
Datasphere tenant's consumption APIs over OAuth 2.0 client-credentials — a
separate path from the SAP BDC zero-copy connector, used here to enrich data-
product / asset discovery.

Configuration (environment variables):
    DATASPHERE_BASE_URL      e.g. https://your-tenant.us10.hcs.cloud.sap
    DATASPHERE_TOKEN_URL     e.g. https://your-tenant.authentication.us10.hana.ondemand.com/oauth/token
    DATASPHERE_CLIENT_ID     OAuth client id (App Integration technical user)
    DATASPHERE_CLIENT_SECRET OAuth client secret
    DATASPHERE_SCOPE         optional OAuth scope
"""

from __future__ import annotations

import base64
import os
import time

import requests


class DatasphereError(RuntimeError):
    """Raised when a Datasphere API call fails."""


class DatasphereClient:
    """Minimal OAuth2 + OData reader for SAP Datasphere consumption APIs."""

    _UA = "SAP-BDC-Snowflake-Console/1.0"

    def __init__(
        self,
        base_url: str | None = None,
        token_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("DATASPHERE_BASE_URL", "")).rstrip("/")
        self.token_url = token_url or os.getenv("DATASPHERE_TOKEN_URL", "")
        self.client_id = client_id or os.getenv("DATASPHERE_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("DATASPHERE_CLIENT_SECRET", "")
        self.scope = scope or os.getenv("DATASPHERE_SCOPE", "")
        self.timeout = timeout
        self._token_val = ""
        self._token_exp = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token_url and self.client_id and self.client_secret)

    # --- auth -----------------------------------------------------------------
    def _token(self) -> str:
        if self._token_val and time.time() < self._token_exp - 60:
            return self._token_val
        if not self.configured:
            raise DatasphereError("Datasphere credentials are not configured.")
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        resp = requests.post(
            self.token_url,
            data=data,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise DatasphereError(f"OAuth token request failed: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        self._token_val = payload["access_token"]
        self._token_exp = time.time() + int(payload.get("expires_in", 3600))
        return self._token_val

    # --- requests -------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, accept: str = "application/json"):
        resp = requests.get(
            self.base_url + path,
            params=params,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": accept,
                "User-Agent": self._UA,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise DatasphereError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json() if accept == "application/json" else resp.text

    # --- catalog reads --------------------------------------------------------
    def list_spaces(self) -> list[dict]:
        """List Datasphere spaces (/consumption/spaces)."""
        data = self._get("/api/v1/datasphere/consumption/spaces")
        return data.get("value", []) if isinstance(data, dict) else []

    def list_assets(self, space_id: str) -> list[dict]:
        """List tables and views exposed for consumption in a space."""
        out: list[dict] = []
        for kind, endpoint in (("table", "tables"), ("view", "views")):
            try:
                data = self._get(
                    f"/api/v1/datasphere/consumption/spaces('{space_id}')/{endpoint}"
                )
            except DatasphereError:
                continue
            for a in (data.get("value", []) if isinstance(data, dict) else []):
                out.append({
                    "space": space_id,
                    "type": kind,
                    "name": a.get("name") or a.get("ID") or a.get("technicalName", ""),
                    "business_name": a.get("displayName") or a.get("businessName", ""),
                })
        return out

    def catalog_summary(self, max_spaces: int = 50) -> dict:
        """Return spaces + per-space asset counts for a discovery overview."""
        spaces = self.list_spaces()
        rows = []
        total_assets = 0
        for s in spaces[:max_spaces]:
            sid = s.get("ID") or s.get("name") or s.get("technicalName") or ""
            if not sid:
                continue
            assets = self.list_assets(sid)
            total_assets += len(assets)
            rows.append({
                "Space": sid,
                "Tables": sum(1 for a in assets if a["type"] == "table"),
                "Views": sum(1 for a in assets if a["type"] == "view"),
                "Assets": len(assets),
            })
        return {"space_count": len(spaces), "total_assets": total_assets, "spaces": rows}
