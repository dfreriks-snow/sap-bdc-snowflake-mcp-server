"""validation_tools.py — Snowflake port of the Databricks extended_tools validation handlers.

Provides 5 MCP tools:
  validate_tenant_hostname      — Pure-logic hostname validation (SAP Notes 3652165 & 3705747)
  validate_share_readiness      — Check Snowflake share + zerocopy connector readiness
  validate_snowflake_privileges — Check current role has required BDC Connect privileges
  check_cld_asset_support       — Scan for object types unsupported by catalog-linked databases
  list_unsupported_share_assets — List objects that cannot be shared via zerocopy connector
"""

import json
import re

from .config import BDCConfig
from .snowflake_client import SnowflakeClient, SnowflakeError, quote_ident, quote_literal

# ---------------------------------------------------------------------------
# Required Snowflake privileges for SAP BDC Connect zero-copy sharing
# ---------------------------------------------------------------------------

_REQUIRED_PRIVILEGES = [
    {
        "privilege": "CREATE ZEROCOPY CONNECTOR",
        "on": "SCHEMA",
        "description": "CREATE ZEROCOPY CONNECTOR on the connector's schema",
    },
    {
        "privilege": "OPERATE",
        "on": "ZEROCOPY CONNECTOR",
        "description": "OPERATE on the zerocopy connector",
    },
    {
        "privilege": "USAGE",
        "on": "ZEROCOPY CONNECTOR",
        "description": "USAGE on the zerocopy connector",
    },
    {
        "privilege": "MODIFY",
        "on": "ZEROCOPY CONNECTOR",
        "description": "MODIFY on the zerocopy connector",
    },
    {
        "privilege": "CREATE DATABASE",
        "on": "ACCOUNT",
        "description": "CREATE DATABASE on the account (for catalog-linked databases)",
    },
    {
        "privilege": "CREATE SHARE",
        "on": "ACCOUNT",
        "description": "CREATE SHARE on the account",
    },
]

# Object kinds unsupported for zero-copy catalog-linked-database consumption
_UNSUPPORTED_CLD_KINDS = frozenset({"MATERIALIZED VIEW", "EXTERNAL TABLE", "DYNAMIC TABLE"})

# Object kinds unsupported for share-back to SAP BDC via zerocopy connector
_UNSUPPORTED_SHARE_KINDS = frozenset({"MATERIALIZED VIEW", "EXTERNAL TABLE"})


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _scan_objects(
    client: SnowflakeClient, database: str, schema: str | None
) -> tuple[list[dict], int, str | None]:
    """Run SHOW OBJECTS IN DATABASE/SCHEMA; return (rows, count, error_or_None)."""
    db_ident = quote_ident(database)
    target = (
        f"SCHEMA {db_ident}.{quote_ident(schema)}" if schema else f"DATABASE {db_ident}"
    )
    try:
        rows = client.execute(f"SHOW OBJECTS IN {target}")
        return rows, len(rows), None
    except SnowflakeError as e:
        return [], 0, str(e)


# ---------------------------------------------------------------------------
# SCHEMAS — MCP tool definitions
# ---------------------------------------------------------------------------

SCHEMAS = [
    {
        "name": "validate_tenant_hostname",
        "description": (
            "Validate a proposed BDC tenant hostname against SAP's rules. "
            "Catches the common failure documented in SAP Notes 3652165 and 3705747 "
            "(uppercase letters cause silent provisioning hangs; duplicate hostnames rejected)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": "The proposed tenant hostname to validate",
                }
            },
            "required": ["hostname"],
        },
    },
    {
        "name": "validate_share_readiness",
        "description": (
            "Validate that a Snowflake share and the SAP BDC Connect zerocopy connector are "
            "ready for BDC integration. Checks share existence, granted objects, connector "
            "status (CONNECTED), and share_back configuration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {
                    "type": "string",
                    "description": "Name of the Snowflake share to validate",
                },
                "check_bdc_registration": {
                    "type": "boolean",
                    "description": (
                        "If true, include a reminder to verify BDC catalog registration "
                        "SAP-side (default: false)"
                    ),
                },
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "validate_snowflake_privileges",
        "description": (
            "Pre-flight check that the current role (or a named principal) has the Snowflake "
            "privileges required for SAP BDC Connect zero-copy sharing: "
            "CREATE ZEROCOPY CONNECTOR (schema), OPERATE/USAGE/MODIFY (zerocopy connector), "
            "CREATE DATABASE and CREATE SHARE (account)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal": {
                    "type": "string",
                    "description": (
                        "Role name to check. Defaults to the current active role if omitted."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "check_cld_asset_support",
        "description": (
            "Scan a Snowflake database (or schema) and flag object types that are NOT supported "
            "for zero-copy catalog-linked-database consumption. "
            "Supported: TABLE, VIEW, ICEBERG TABLE. "
            "Flagged: MATERIALIZED VIEW, EXTERNAL TABLE, DYNAMIC TABLE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "description": "Database to scan",
                },
                "schema": {
                    "type": "string",
                    "description": (
                        "Optional schema name within the database. "
                        "If omitted, scans all objects in the database."
                    ),
                },
            },
            "required": ["database"],
        },
    },
    {
        "name": "list_unsupported_share_assets",
        "description": (
            "Scan a Snowflake database (or schema) and list assets that cannot be shared to "
            "SAP BDC via the zero-copy connector. Returns full_name, type, and a resolution hint."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "description": "Database to scan",
                },
                "schema": {
                    "type": "string",
                    "description": (
                        "Optional schema name. If omitted, scans all objects in the database."
                    ),
                },
            },
            "required": ["database"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_validate_tenant_hostname(arguments: dict, client, cfg) -> str:
    """Validate proposed tenant hostname per SAP rules (SAP Notes 3652165 & 3705747).

    Ported verbatim from the Databricks extended_tools.py — pure logic, platform-neutral.
    """
    hostname = arguments["hostname"]
    errors = []
    warnings = []

    if not hostname:
        return "❌ Hostname is empty."

    if len(hostname) < 3:
        errors.append("Hostname is too short (minimum 3 characters recommended).")
    if len(hostname) > 63:
        errors.append("Hostname exceeds 63 characters — most SAP host validators reject this.")

    if hostname != hostname.lower():
        errors.append(
            "❌ Hostname contains uppercase letters. Per SAP Note 3652165 this causes "
            "BDC Core provisioning to hang silently. Use only [a-z0-9-]."
        )

    if not re.fullmatch(r"[a-z0-9-]+", hostname):
        errors.append(
            "❌ Hostname contains characters outside [a-z0-9-]. Remove underscores, dots, "
            "uppercase, or other special chars."
        )

    if hostname.startswith("-") or hostname.endswith("-"):
        errors.append("❌ Hostname must not start or end with a hyphen.")

    if "--" in hostname:
        warnings.append("⚠️  Double hyphens are allowed but discouraged for readability.")

    warnings.append(
        "ℹ️  SAP Note 3705747: hostnames must be unique within the region. "
        "This tool cannot verify uniqueness remotely — try the provisioning and watch for "
        "'The host name XXX is already being used'."
    )

    if errors:
        return (
            f"❌ Hostname '{hostname}' is INVALID:\n"
            + "\n".join(f"  {e}" for e in errors)
            + "\n\nWarnings:\n"
            + "\n".join(f"  {w}" for w in warnings)
        )

    return (
        f"✅ Hostname '{hostname}' passes syntactic validation.\n\n"
        + "\n".join(f"  {w}" for w in warnings)
    )


def handle_validate_share_readiness(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    """Check Snowflake share existence/objects and zerocopy connector readiness for BDC."""
    share_name = arguments["share_name"]
    check_bdc = arguments.get("check_bdc_registration", False)

    checks: dict = {}
    warnings: list = []
    errors: list = []
    ready = True

    # --- Check 1: Share exists and has granted objects ---
    try:
        share_rows = client.execute(f"DESC SHARE {quote_ident(share_name)}")
        object_count = len(share_rows)
        checks["share_exists"] = {
            "status": "✅ PASS",
            "message": f"Share '{share_name}' exists with {object_count} granted object(s).",
        }
        if object_count == 0:
            warnings.append(
                f"⚠️  Share '{share_name}' has no granted objects. "
                "Add tables/views with: GRANT SELECT ON TABLE ... TO SHARE ..."
            )
            checks["share_has_objects"] = {
                "status": "⚠️  WARN",
                "message": "Share has 0 objects — BDC consumers will see an empty data product.",
            }
        else:
            checks["share_has_objects"] = {
                "status": "✅ PASS",
                "message": f"Share has {object_count} object(s).",
            }
    except SnowflakeError as e:
        ready = False
        checks["share_exists"] = {
            "status": "❌ FAIL",
            "message": f"Share '{share_name}' not found or not accessible.",
            "error": str(e),
        }
        errors.append(f"Share '{share_name}' not found: {e}")

    # --- Check 2: Zerocopy connector status and share_back ---
    # Use SHOW ZEROCOPY CONNECTORS IN ACCOUNT (known column layout: name, status, share_back)
    # and filter to the connector matching cfg.connector_name.
    try:
        connector_rows = client.execute("SHOW ZEROCOPY CONNECTORS IN ACCOUNT")
        matched = [
            c for c in connector_rows
            if str(c.get("name") or "").upper() == cfg.connector_name.upper()
        ]
        if not matched:
            ready = False
            checks["connector_status"] = {
                "status": "❌ FAIL",
                "message": (
                    f"Connector '{cfg.connector_name}' not found via "
                    "SHOW ZEROCOPY CONNECTORS IN ACCOUNT. "
                    "Verify connector name in config and that you have USAGE on it."
                ),
            }
            errors.append(f"Connector '{cfg.connector_name}' not found.")
        else:
            c = matched[0]
            status_val = str(c.get("status") or "").upper()
            share_back_val = str(c.get("share_back") or "").upper()

            if status_val == "CONNECTED":
                checks["connector_status"] = {
                    "status": "✅ PASS",
                    "message": f"Connector '{cfg.connector_fqn}' is CONNECTED.",
                }
            else:
                ready = False
                checks["connector_status"] = {
                    "status": "❌ FAIL",
                    "message": (
                        f"Connector '{cfg.connector_fqn}' status is '{status_val or 'UNKNOWN'}'. "
                        "Expected CONNECTED. Check connector credentials and BDC endpoint."
                    ),
                }
                errors.append(f"Connector not CONNECTED (status={status_val!r})")

            if share_back_val in ("TRUE", "ENABLED", "YES", "1"):
                checks["share_back_enabled"] = {
                    "status": "✅ PASS",
                    "message": "share_back is enabled on the connector.",
                }
            else:
                warnings.append(
                    f"⚠️  share_back is not enabled (value={share_back_val!r}). "
                    f"Run: ALTER ZEROCOPY CONNECTOR {cfg.connector_fqn} SET SHARE_BACK = TRUE"
                )
                checks["share_back_enabled"] = {
                    "status": "⚠️  WARN",
                    "message": (
                        f"share_back value='{share_back_val}' — "
                        "BDC will not be able to discover this share until share_back is enabled."
                    ),
                }
    except SnowflakeError as e:
        ready = False
        checks["connector_status"] = {
            "status": "❌ FAIL",
            "message": f"Could not query zerocopy connectors: {e}",
            "error": str(e),
        }
        errors.append(f"Connector check failed: {e}")

    # --- Check 3 (optional): BDC catalog registration reminder ---
    if check_bdc:
        checks["bdc_registration"] = {
            "status": "ℹ️  INFO",
            "message": (
                "BDC catalog registration must be verified on the SAP side. "
                "Confirm the data product is published and active in the BDC catalog "
                "via the SAP BDC cockpit or the BDC Connect API."
            ),
        }

    result = {
        "share_name": share_name,
        "ready_for_bdc": ready,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }

    status_line = (
        "✅ Share is ready for SAP BDC."
        if ready
        else "❌ Share is NOT ready — see errors above."
    )
    return f"{status_line}\n\n{json.dumps(result, indent=2)}"


def handle_validate_snowflake_privileges(
    arguments: dict, client: SnowflakeClient, cfg: BDCConfig
) -> str:
    """Pre-flight: confirm the current role has required BDC Connect privileges."""
    principal = (arguments.get("principal") or "").strip() or None

    try:
        if not principal:
            principal = client.execute_scalar("SELECT CURRENT_ROLE()")
            if not principal:
                return "❌ Could not determine current role."

        rows = client.execute(f"SHOW GRANTS TO ROLE {quote_ident(principal)}")
    except SnowflakeError as e:
        return f"❌ Could not query grants for role '{principal}': {e}"

    # Build lookup sets keyed by (privilege_upper, granted_on_upper)
    granted_on_account: set[str] = set()
    granted_on_schema: set[str] = set()
    granted_on_connector: set[str] = set()

    for row in rows:
        priv = str(row.get("privilege") or "").upper().strip()
        on_type = str(row.get("granted_on") or "").upper().strip()
        if on_type == "ACCOUNT":
            granted_on_account.add(priv)
        elif on_type == "SCHEMA":
            granted_on_schema.add(priv)
        elif "CONNECTOR" in on_type:
            granted_on_connector.add(priv)

    # ACCOUNTADMIN implicitly holds all account/connector privileges; SHOW GRANTS
    # does not enumerate inherited/ownership-based privileges, so treat it specially.
    is_admin = str(principal).upper() in ("ACCOUNTADMIN", "SECURITYADMIN")

    table_rows = []
    all_ok = True

    for req in _REQUIRED_PRIVILEGES:
        priv = req["privilege"]
        on = req["on"]
        desc = req["description"]

        if is_admin:
            present = True
        elif on == "ACCOUNT":
            present = priv in granted_on_account
        elif on == "SCHEMA":
            present = priv in granted_on_schema
        elif on == "ZEROCOPY CONNECTOR":
            present = priv in granted_on_connector
        else:
            present = False

        status = "✅" if present else "❌"
        if not present:
            all_ok = False
        table_rows.append(f"  {status}  {priv:<30}  on {on:<22}  ({desc})")

    summary_line = (
        f"✅ Role '{principal}' has all required BDC Connect privileges."
        if all_ok
        else f"❌ Role '{principal}' is MISSING required privileges — see table below."
    )

    hint = ""
    if not all_ok:
        hint = (
            "\n\nTo grant missing account-level privileges (run as ACCOUNTADMIN):\n"
            f"  GRANT CREATE DATABASE ON ACCOUNT TO ROLE {quote_ident(principal)};\n"
            f"  GRANT CREATE SHARE ON ACCOUNT TO ROLE {quote_ident(principal)};\n"
            "\nTo grant connector-level privileges (run as connector owner or ACCOUNTADMIN):\n"
            f"  GRANT OPERATE, USAGE, MODIFY ON ZEROCOPY CONNECTOR {cfg.connector_fqn}"
            f" TO ROLE {quote_ident(principal)};\n"
            "\nTo grant schema-level privileges:\n"
            f"  GRANT CREATE ZEROCOPY CONNECTOR ON SCHEMA"
            f" {cfg.connector_database}.{cfg.connector_schema}"
            f" TO ROLE {quote_ident(principal)};"
        )

    return summary_line + "\n\nPrivilege Check:\n" + "\n".join(table_rows) + hint


def handle_check_cld_asset_support(
    arguments: dict, client: SnowflakeClient, cfg: BDCConfig
) -> str:
    """Scan for object types not supported in zero-copy catalog-linked databases."""
    database = (arguments.get("database") or "").strip()
    schema = (arguments.get("schema") or "").strip() or None

    if not database:
        return "❌ 'database' argument is required."

    rows, total, err = _scan_objects(client, database, schema)
    if err:
        return f"❌ Could not scan objects in {'schema' if schema else 'database'}: {err}"

    _hint_cld: dict[str, str] = {
        "MATERIALIZED VIEW": (
            "Re-expose as a regular VIEW or persist as a TABLE before linking."
        ),
        "EXTERNAL TABLE": (
            "Convert to an Iceberg table or a regular TABLE; external tables are not "
            "supported in catalog-linked databases."
        ),
        "DYNAMIC TABLE": (
            "Dynamic Tables are not supported in catalog-linked databases; "
            "persist the result as a TABLE instead."
        ),
    }

    flagged = []
    for row in rows:
        kind = str(row.get("kind") or "").upper().strip()
        if kind not in _UNSUPPORTED_CLD_KINDS:
            continue
        name = row.get("name") or ""
        schema_name = row.get("schema_name") or schema or ""
        full_name = (
            f"{database}.{schema_name}.{name}" if schema_name else f"{database}.{name}"
        )
        flagged.append({
            "full_name": full_name,
            "type": kind,
            "hint": _hint_cld.get(kind, "Not supported in catalog-linked databases."),
        })

    result = {
        "database": database,
        "schema": schema,
        "inspected_count": total,
        "unsupported_count": len(flagged),
        "unsupported": flagged,
        "supported_kinds": ["TABLE", "VIEW", "ICEBERG TABLE"],
        "unsupported_kinds": sorted(_UNSUPPORTED_CLD_KINDS),
    }

    if flagged:
        return (
            f"❌ Found {len(flagged)} unsupported object(s) out of {total} inspected.\n\n"
            + json.dumps(result, indent=2)
        )
    return (
        f"✅ All {total} object(s) are supported for zero-copy catalog-linked database consumption.\n\n"
        + json.dumps(result, indent=2)
    )


def handle_list_unsupported_share_assets(
    arguments: dict, client: SnowflakeClient, cfg: BDCConfig
) -> str:
    """List objects that cannot be shared to SAP BDC via the zero-copy connector."""
    database = (arguments.get("database") or "").strip()
    schema = (arguments.get("schema") or "").strip() or None

    if not database:
        return "❌ 'database' argument is required."

    rows, total, err = _scan_objects(client, database, schema)
    if err:
        return f"❌ Could not scan objects: {err}"

    _hint_share: dict[str, str] = {
        "MATERIALIZED VIEW": (
            "Re-expose as a regular VIEW, or persist as a Delta/Iceberg table "
            "before adding to the share."
        ),
        "EXTERNAL TABLE": (
            "Convert to a native TABLE or Iceberg table; external tables cannot be "
            "added to Snowflake shares."
        ),
    }

    unsupported = []
    for row in rows:
        kind = str(row.get("kind") or "").upper().strip()
        if kind not in _UNSUPPORTED_SHARE_KINDS:
            continue
        name = row.get("name") or ""
        schema_name = row.get("schema_name") or schema or ""
        full_name = (
            f"{database}.{schema_name}.{name}" if schema_name else f"{database}.{name}"
        )
        unsupported.append({
            "full_name": full_name,
            "type": kind,
            "hint": _hint_share.get(kind, "Not supported in Snowflake data shares."),
        })

    result = {
        "database": database,
        "schema": schema,
        "inspected_count": total,
        "unsupported_count": len(unsupported),
        "unsupported": unsupported,
        "source": "SAP BDC Connect for Snowflake — zero-copy connector sharing requirements",
    }

    if unsupported:
        return (
            f"❌ Found {len(unsupported)} asset(s) that cannot be shared to SAP BDC "
            f"via the zero-copy connector.\n\n"
            + json.dumps(result, indent=2)
        )
    return (
        f"✅ All {total} object(s) can be shared to SAP BDC via the zero-copy connector.\n\n"
        + json.dumps(result, indent=2)
    )


# ---------------------------------------------------------------------------
# Module-level dispatch map
# ---------------------------------------------------------------------------

HANDLERS: dict = {
    "validate_tenant_hostname": handle_validate_tenant_hostname,
    "validate_share_readiness": handle_validate_share_readiness,
    "validate_snowflake_privileges": handle_validate_snowflake_privileges,
    "check_cld_asset_support": handle_check_cld_asset_support,
    "list_unsupported_share_assets": handle_list_unsupported_share_assets,
}
