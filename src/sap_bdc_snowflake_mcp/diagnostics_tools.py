"""Diagnostics tools for the SAP BDC Snowflake MCP Server.

Ported from sap-bdc-mcp-server extended_tools.py (Databricks → Snowflake).
Tools:
    - diagnose_share_error:          Map error text to SAP Note + resolution
    - cleanup_orphaned_data_product: Handle SAP Note 3720724 orphan scenario (Snowflake)
"""

import json
import re

from .config import BDCConfig
from .snowflake_client import SnowflakeClient, SnowflakeError, quote_ident, quote_literal


# ---------------------------------------------------------------------------
# Symptom → SAP Note map (ported verbatim from extended_tools.py)
# ---------------------------------------------------------------------------

_DIAGNOSTIC_RULES = [
    {
        "patterns": [r"oidc[_\s]+code[_\s]+exchange[_\s]+failure",
                     r"error\s+logging\s+you\s+in.*oidc"],
        "note": "SAP Note 3678584",
        "title": "SAP Databricks OIDC Code Exchange Failure",
        "cause": "Databricks client ID/secret pair expires every 6 months.",
        "resolution": (
            "1. Exclude the affected IAS tenant from the Formation type "
            "'Integration with SAP Databricks'.\n"
            "2. Re-include the IAS tenant (regenerates the client secret).\n"
            "3. Retry login."
        ),
    },
    {
        "patterns": [r"unable\s+to\s+serve\s+your\s+request",
                     r"errorCode.*500.*create_or_update_share_csn",
                     r"HTTP response body.*\"errorCode\":500"],
        "note": "SAP Notes 3706399 & 3717031",
        "title": "Generic 500 error when publishing CSN / Delta Share to BDC",
        "cause": (
            "Possible causes:\n"
            "  - Deletion Vectors enabled on shared tables (3706399)\n"
            "  - Missing permissions on the share or recipient\n"
            "  - Assets missing on the shared table\n"
            "  - Metastore admin changed"
        ),
        "resolution": (
            "1. Run check_deletion_vectors(share_name=...) to rule out 3706399.\n"
            "2. Verify SHOW GRANTS ON SHARE <share> includes the BDC recipient.\n"
            "3. Verify all referenced tables still exist and are accessible.\n"
            "4. Confirm the metastore admin has not changed."
        ),
    },
    {
        "patterns": [r"only\s+one\s+replace\s+operation\s+is\s+allowed",
                     r"scimType.*invalidValue"],
        "note": "SAP Note 3738570",
        "title": "Databricks SCIM rejects SAP IPS multi-replace PATCH",
        "cause": "Databricks SCIM allows only one replace op per PATCH; SAP IPS sends multiple.",
        "resolution": (
            "Apply the workaround in SAP Note 3738570 (IPS transformation to split PATCHes) "
            "or provision the users one at a time until Databricks lifts the restriction."
        ),
    },
    {
        "patterns": [r"sap\s+cloud\s+identity\s+service\s+integration\s+not\s+configured",
                     r"already\s+part\s+of\s+another\s+formation"],
        "note": "SAP Notes 3706392 & 3694878",
        "title": "CIS integration not configured on SAP Databricks",
        "cause": (
            "Either:\n"
            "  - CIS integration is genuinely not yet configured (3706392), or\n"
            "  - You are trying to use the same CIS for a second Databricks tenant (3694878), "
            "which is not supported in a single formation."
        ),
        "resolution": (
            "For 3706392: wait up to 30 minutes after provisioning, or open a support case "
            "with component BDC-DBX-CON.\n"
            "For 3694878: provision the second Databricks tenant with a different CIS, or join "
            "it to a new formation."
        ),
    },
    {
        "patterns": [r"the\s+host\s+name\s+\S+\s+is\s+already\s+being\s+used"],
        "note": "SAP Note 3705747",
        "title": "Duplicate hostname during BDC Cockpit provisioning",
        "cause": "Hostname already registered by another user in the same region.",
        "resolution": "Choose a unique hostname. Use validate_tenant_hostname() to precheck.",
    },
    {
        "patterns": [r"provisioning.*hangs", r"provisioning.*stuck",
                     r"taking\s+long.*provisioning"],
        "note": "SAP Note 3652165",
        "title": "BDC Core provisioning hangs",
        "cause": "tenantHostName contains uppercase letters — silent failure.",
        "resolution": (
            "Delete the failed instance; recreate with a hostname in [a-z0-9-] only. "
            "Use validate_tenant_hostname() to precheck."
        ),
    },
    {
        "patterns": [r"orphan(ed)?\s+data\s+product", r"data\s+product.*active.*after.*delete"],
        "note": "SAP Note 3720724",
        "title": "Orphan data product in BDC catalog",
        "cause": "Delta share was deleted in Databricks UI, BDC catalog still sees the Data Product.",
        "resolution": "Use cleanup_orphaned_data_product() or open SAP support quoting 3720724.",
    },
    {
        "patterns": [r"-0001-11-30", r"negative\s+date", r"00000000.*date"],
        "note": "SAP Note 3736857",
        "title": "Null SAP dates become negative dates in Databricks",
        "cause": "SAP blank date '00000000' is parsed by Spark as year -1, month 11, day 30.",
        "resolution": (
            "In your Transformation Flow, coerce '00000000' (or equivalent null sentinels) "
            "to true SQL NULL before the downstream date cast."
        ),
    },
    {
        "patterns": [r"business\s+name.*missing.*unity\s+catalog"],
        "note": "SAP Note 3725086",
        "title": "Business Name missing in Unity Catalog",
        "cause": "Historical limitation — Databricks now supports this metadata.",
        "resolution": (
            "Ensure the SAP Databricks / Enterprise Databricks cluster runtime is recent enough; "
            "the fix is delivered. Re-mount the share if necessary."
        ),
    },
    {
        "patterns": [r"hana\s+cloud.*not\s+listed.*customer\s+landscape"],
        "note": "SAP Note 3731036",
        "title": "HANA Cloud instance not visible in BDC Customer Landscape",
        "cause": "SAP HANA Cloud Central is not running in the subaccount.",
        "resolution": (
            "Ensure HANA Cloud Central is active in the subaccount — it acts as the Data Product "
            "gateway for all HANA instances in that subaccount."
        ),
    },
]

# Snowflake zero-copy connector state hints (appended when connector state keywords appear)
_CONNECTOR_STATE_RULES = [
    {
        "patterns": [r"\bCONNECT_ERROR\b"],
        "note": "Snowflake Zero-Copy Connector: CONNECT_ERROR",
        "title": "Connector failed to connect to SAP BDC",
        "cause": "The SAP BDC invitation link may be expired or the connector configuration is invalid.",
        "resolution": (
            "Retry: ALTER ZEROCOPY CONNECTOR <name> CONNECT WITH CONFIG=(INVITATION_LINK='...')\n"
            "If the link is expired, generate a new invitation in the SAP BDC Cockpit.\n"
            "Alternatively, DROP and recreate the connector with a fresh invitation link."
        ),
    },
    {
        "patterns": [r"\bDISCONNECT_ERROR\b"],
        "note": "Snowflake Zero-Copy Connector: DISCONNECT_ERROR",
        "title": "Connector failed to disconnect cleanly",
        "cause": "The disconnect operation encountered an error on the SAP BDC side.",
        "resolution": (
            "Retry: ALTER ZEROCOPY CONNECTOR <name> DISCONNECT\n"
            "If it persists, open an SAP support case."
        ),
    },
    {
        "patterns": [r"\bnot\s+connected\b", r"\bCONNECTING\b"],
        "note": "Snowflake Zero-Copy Connector: not yet connected",
        "title": "Connector is still establishing connection",
        "cause": "The connector is in a transient CONNECTING state or has not been connected yet.",
        "resolution": (
            "Wait a moment and inspect the connector status:\n"
            "  DESC ZEROCOPY CONNECTOR <name>;\n"
            "Once the state is CONNECTED, retry your operation."
        ),
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_diagnose_share_error(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    """Map an error message to the relevant SAP Note and resolution."""
    error = arguments["error_message"]
    context = arguments.get("context", "")
    haystack = (error + " " + context).lower()

    hits = []
    for rule in _DIAGNOSTIC_RULES:
        for pat in rule["patterns"]:
            if re.search(pat, haystack, re.IGNORECASE):
                hits.append(rule)
                break

    # Snowflake connector-state awareness: check against connector state rules
    for rule in _CONNECTOR_STATE_RULES:
        for pat in rule["patterns"]:
            if re.search(pat, haystack, re.IGNORECASE):
                hits.append(rule)
                break

    if not hits:
        return (
            "No matching SAP Note found for this error message.\n\n"
            "Recommended next steps:\n"
            "  - Open SAP Note 3653192 (main Databricks/BDC troubleshooting guide)\n"
            "  - Follow SAP Note 3568017 for guidance on opening a support case\n"
            "  - Include the full stack trace, request ID, and timestamp in your case"
        )

    parts = [f"Found {len(hits)} matching SAP Note(s):\n"]
    for h in hits:
        parts.append(
            f"\n--- {h['note']}: {h['title']} ---\n"
            f"Cause: {h['cause']}\n"
            f"Resolution:\n{h['resolution']}\n"
        )
    return "\n".join(parts)


def handle_cleanup_orphaned_data_product(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    """Handle orphaned data product scenario (SAP Note 3720724) — Snowflake edition.

    Scenario: a Snowflake SHARE was dropped manually but the Data Product remains
    Active in the BDC catalog and cannot be unpublished normally.
    """
    share_name = arguments["share_name"]
    force = arguments.get("force", False)

    connector_fqn = cfg.connector_fqn
    quoted_share = quote_ident(share_name)

    guidance = (
        f"SAP Note 3720724: Orphaned data product '{share_name}'\n\n"
        "Scenario: The Snowflake SHARE was dropped manually but the Data Product remains\n"
        "'Active' in the BDC catalog and normal unpublish does not work.\n\n"
        "Recommended resolution steps (Snowflake):\n"
        f"  1. Recreate an empty share with the same name:\n"
        f"       CREATE SHARE IF NOT EXISTS {quoted_share};\n\n"
        f"  2. Re-associate with the zero-copy connector:\n"
        f"       ALTER ZEROCOPY CONNECTOR {connector_fqn} ADD SHARE {quoted_share};\n\n"
        f"  3. Unpublish in the BDC Cockpit (removes the Data Product from the catalog),\n"
        f"     then remove the share from the connector and drop it:\n"
        f"       ALTER ZEROCOPY CONNECTOR {connector_fqn} REMOVE SHARE {quoted_share};\n"
        f"       DROP SHARE IF EXISTS {quoted_share};\n\n"
        "  4. If the Data Product still shows Active in BDC after the above steps,\n"
        "     open an SAP support case quoting SAP Note 3720724."
    )

    if not force:
        return guidance

    # force=True: best-effort execution of each step, report per-step results
    results = []

    # Step 1: recreate empty share
    try:
        client.execute(f"CREATE SHARE IF NOT EXISTS {quoted_share}")
        results.append(f"  [OK] CREATE SHARE IF NOT EXISTS {quoted_share}")
    except SnowflakeError as e:
        results.append(f"  [FAIL] CREATE SHARE IF NOT EXISTS {quoted_share}: {e}")

    # Step 2: remove share from connector (prep for drop)
    try:
        client.execute(f"ALTER ZEROCOPY CONNECTOR {connector_fqn} REMOVE SHARE {quoted_share}")
        results.append(f"  [OK] ALTER ZEROCOPY CONNECTOR {connector_fqn} REMOVE SHARE {quoted_share}")
    except SnowflakeError as e:
        results.append(f"  [FAIL] ALTER ZEROCOPY CONNECTOR ... REMOVE SHARE: {e}")

    # Step 3: drop the share
    try:
        client.execute(f"DROP SHARE IF EXISTS {quoted_share}")
        results.append(f"  [OK] DROP SHARE IF EXISTS {quoted_share}")
    except SnowflakeError as e:
        results.append(f"  [FAIL] DROP SHARE IF EXISTS {quoted_share}: {e}")

    step_summary = "\n".join(results)
    return (
        f"Force cleanup attempted for '{share_name}':\n"
        f"{step_summary}\n\n"
        "If the Data Product still appears Active in the BDC Cockpit, follow the manual "
        "guidance below.\n\n"
        + guidance
    )


# ---------------------------------------------------------------------------
# Module contract: SCHEMAS and HANDLERS
# ---------------------------------------------------------------------------

SCHEMAS = [
    {
        "name": "diagnose_share_error",
        "description": (
            "Map an SAP BDC / Snowflake error message to the relevant SAP Note and "
            "resolution steps. Knows about OIDC code exchange failures, error 500s on CSN "
            "sharing, SCIM 'only one replace' errors, CIS integration issues, Snowflake "
            "zero-copy connector state errors (CONNECT_ERROR, DISCONNECT_ERROR, CONNECTING), "
            "and more."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "error_message": {
                    "type": "string",
                    "description": "The error message or error text to diagnose",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context (e.g., 'happened during share publish')",
                },
            },
            "required": ["error_message"],
        },
    },
    {
        "name": "cleanup_orphaned_data_product",
        "description": (
            "Handle the orphan scenario from SAP Note 3720724: a Snowflake SHARE was dropped "
            "manually, but the Data Product remains 'Active' in the BDC Catalog and cannot be "
            "unpublished normally. Returns Snowflake-specific remediation steps and, if "
            "force=true, attempts best-effort SQL cleanup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {
                    "type": "string",
                    "description": "Name of the orphaned share / data product",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Attempt SQL-level cleanup via CREATE SHARE / REMOVE SHARE / DROP SHARE "
                        "(default: false — produces guidance only)"
                    ),
                },
            },
            "required": ["share_name"],
        },
    },
]

HANDLERS = {
    "diagnose_share_error": handle_diagnose_share_error,
    "cleanup_orphaned_data_product": handle_cleanup_orphaned_data_product,
}
