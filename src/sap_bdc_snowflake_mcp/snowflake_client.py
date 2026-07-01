"""Snowflake execution layer for the SAP BDC Snowflake MCP server.

Replaces the Databricks WorkspaceClient / bdc_connect_sdk in the original repo.
Connects with snowflake-connector-python and runs the SQL that drives the SAP
BDC Connect zero-copy connector (CREATE/ALTER/DROP/DESC ZEROCOPY CONNECTOR,
SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES, SHOW/DESC SHARE, SHOW GRANTS, etc.).

Auth: a named connection from ~/.snowflake/connections.toml (recommended;
key-pair/SSO/password all supported) or discrete SNOWFLAKE_* parameters.
"""

from __future__ import annotations

import os
import threading
import tomllib
from pathlib import Path
from typing import Any, Optional

from .config import BDCConfig


class SnowflakeError(RuntimeError):
    """Raised when a Snowflake statement fails."""


class SnowflakeClient:
    """Thin Snowflake client: connect once, execute SQL, return dict rows."""

    def __init__(self, cfg: BDCConfig) -> None:
        self.cfg = cfg
        self._conn = None
        self._lock = threading.Lock()

    # ----- connection --------------------------------------------------------
    def _connect_kwargs(self) -> dict:
        """Build snowflake.connector.connect kwargs from config / connections.toml."""
        # Named connection: read connections.toml and map key-pair path correctly.
        if self.cfg.connection_name:
            path = Path(os.path.expanduser("~/.snowflake/connections.toml"))
            if path.exists():
                with open(path, "rb") as fh:
                    conf = tomllib.load(fh)
                params = dict(conf.get(self.cfg.connection_name, {}))
                if params:
                    if "private_key_path" in params:
                        params["private_key_file"] = os.path.expanduser(
                            params.pop("private_key_path")
                        )
                    # Ensure a current database + schema so account-level system
                    # functions (e.g. SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES) have context.
                    if "database" not in params and self.cfg.connector_database:
                        params["database"] = self.cfg.connector_database
                    if "schema" not in params and self.cfg.connector_schema:
                        params["schema"] = self.cfg.connector_schema
                    return params
            # Fall back to letting the connector resolve the named connection.
            return {"connection_name": self.cfg.connection_name}

        # Discrete parameters.
        params: dict[str, Any] = {}
        for key in ("account", "user", "role", "warehouse", "database", "schema"):
            val = getattr(self.cfg, key, None)
            if val:
                params[key] = val
        # Auth: password or key-pair or externalbrowser from env.
        if os.getenv("SNOWFLAKE_PASSWORD"):
            params["password"] = os.getenv("SNOWFLAKE_PASSWORD")
        if os.getenv("SNOWFLAKE_AUTHENTICATOR"):
            params["authenticator"] = os.getenv("SNOWFLAKE_AUTHENTICATOR")
        if os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"):
            params["private_key_file"] = os.path.expanduser(
                os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
            )
        return params

    def connect(self):
        if self._conn is not None:
            return self._conn
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                import snowflake.connector  # noqa: WPS433
                self._conn = snowflake.connector.connect(**self._connect_kwargs())
            except Exception as exc:  # pragma: no cover - connection errors
                raise SnowflakeError(
                    f"Failed to connect to Snowflake: {exc}. Check SNOWFLAKE_CONNECTION "
                    f"or SNOWFLAKE_* env vars and ~/.snowflake/connections.toml."
                )
            return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ----- execution ---------------------------------------------------------
    def execute(self, sql: str, params: Optional[Any] = None) -> list[dict]:
        """Run a statement and return rows as a list of dicts."""
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description] if cur.description else []
            rows = cur.fetchall() if cols else []
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            raise SnowflakeError(f"{exc}")
        finally:
            cur.close()

    def execute_scalar(self, sql: str, params: Optional[Any] = None):
        """Run a statement and return the first column of the first row (or None)."""
        rows = self.execute(sql, params)
        if not rows:
            return None
        first = rows[0]
        return next(iter(first.values()), None)

    def compile(self, sql: str) -> Optional[str]:
        """Validate a statement compiles without executing side effects.

        Uses EXPLAIN for supported DML/queries. DDL (CREATE/ALTER/DROP) and SHOW
        cannot be EXPLAINed, so returns None (skip) for those.
        """
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE"):
            try:
                self.execute(f"EXPLAIN USING TEXT {sql}")
                return "ok"
            except SnowflakeError as exc:
                return f"error: {exc}"
        return None

    # ----- Cortex helper (optional AI) ---------------------------------------
    def cortex_complete(self, model: str, prompt: str) -> Optional[str]:
        """Call SNOWFLAKE.CORTEX.COMPLETE; returns None on any failure."""
        try:
            return self.execute_scalar(
                "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (model, prompt)
            )
        except SnowflakeError:
            return None


# ---------------------------------------------------------------------------
# Identifier quoting helpers (shared by tool modules)
# ---------------------------------------------------------------------------
def quote_ident(name: str) -> str:
    """Safely quote a Snowflake identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Safely quote a Snowflake string literal."""
    return "'" + str(value).replace("'", "''") + "'"
