"""Offline tests for the SAP BDC Snowflake MCP server.

These tests do not require a live Snowflake connection — they validate the tool
registry contract and the pure-logic handlers (tenant hostname, ORD metadata,
error diagnostics), using a fake client where a handler needs one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sap_bdc_snowflake_mcp import (  # noqa: E402
    connector_tools, diagnostics_tools, metadata_tools, validation_tools,
)
from sap_bdc_snowflake_mcp.config import BDCConfig  # noqa: E402

_MODULES = (connector_tools, validation_tools, metadata_tools, diagnostics_tools)
CFG = BDCConfig()


class FakeClient:
    """Minimal client that returns canned rows (no Snowflake needed)."""

    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def execute(self, sql, params=None):
        return self._rows

    def execute_scalar(self, sql, params=None):
        return self._scalar


def test_seventeen_tools_registered():
    names = []
    for m in _MODULES:
        names += [s["name"] for s in m.SCHEMAS]
    assert len(names) == 17, f"expected 17 tools, got {len(names)}"
    assert len(set(names)) == 17, "duplicate tool names"


def test_schema_handler_contract():
    for m in _MODULES:
        schema_names = {s["name"] for s in m.SCHEMAS}
        assert schema_names == set(m.HANDLERS), f"{m.__name__} schema/handler mismatch"
        for s in m.SCHEMAS:
            assert {"name", "description", "inputSchema"} <= set(s)


def test_validate_tenant_hostname_rules():
    h = validation_tools.HANDLERS["validate_tenant_hostname"]
    assert "INVALID" in h({"hostname": "MyHost"}, None, CFG)      # uppercase
    assert "INVALID" in h({"hostname": "bad_host"}, None, CFG)    # underscore
    ok = h({"hostname": "my-bdc-tenant"}, None, CFG)
    assert "✅" in ok or "passes" in ok.lower()


def test_validate_ord_metadata_required_fields():
    h = metadata_tools.HANDLERS["validate_ord_metadata"]
    bad = h({"ord": {"title": "X"}}, None, CFG)   # missing shortDescription/description
    assert "❌" in bad or "missing" in bad.lower()


def test_diagnose_share_error_matches_sap_note():
    h = diagnostics_tools.HANDLERS["diagnose_share_error"]
    out = h({"error_message": "OIDC code exchange failure while logging in"}, None, CFG)
    assert "SAP Note" in out


def test_generate_csn_template_from_share():
    h = metadata_tools.HANDLERS["generate_csn_template"]
    client = FakeClient(rows=[{"kind": "TABLE", "name": "DB.SCH.CUSTOMERS"}])
    out = h({"share_name": "S"}, client, CFG)
    assert "CUSTOMERS" in out and "definitions" in out


def test_check_cld_asset_support_discovers_existing_clds():
    h = validation_tools.HANDLERS["check_cld_asset_support"]
    client = FakeClient(rows=[
        {"name": "STD_DB", "kind": "STANDARD", "owner": "R", "created_on": None, "comment": None},
        {"name": "SAP_HR_V1", "kind": "CATALOG-LINKED DATABASE", "owner": "R",
         "created_on": None, "comment": None},
    ])
    out = h({}, client, CFG)  # no database → discovery mode
    assert "SAP_HR_V1" in out
    assert "STD_DB" not in out
    assert '"catalog_linked_database_count": 1' in out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
