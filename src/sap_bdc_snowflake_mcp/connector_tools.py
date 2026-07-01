"""Zero-copy connector + share / data-product tools.

Snowflake port of the original Databricks Delta-Sharing tools. Shares are
Snowflake SHAREs associated with the SAP BDC Connect zero-copy connector
(``share_back``); inbound SAP data products are discovered through
``SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES`` and consumed as catalog-linked
databases (CLD).

Each handler has signature ``handle_x(arguments, client, cfg) -> str``.
"""

from __future__ import annotations

import json

from .config import BDCConfig
from .snowflake_client import SnowflakeClient, SnowflakeError, quote_ident, quote_literal


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
SCHEMAS = [
    {
        "name": "list_shares",
        "description": "List Snowflake shares on the account (the zero-copy 'share-back' data "
                       "products published to SAP BDC). Returns name, kind, database, owner, comment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max shares to return (default 100)"}
            },
        },
    },
    {
        "name": "get_share_details",
        "description": "Describe a Snowflake share: the objects (databases/schemas/tables) granted to it "
                       "and its accounts/comment. Snowflake equivalent of Databricks get_share.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share to inspect"}
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "list_recipients",
        "description": "List the SAP BDC Connect zero-copy connector(s) — the Snowflake analog of Delta "
                       "Sharing recipients — plus the inbound SAP data products available to consume "
                       "(via SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_or_update_share",
        "description": "Create or update a Snowflake share for distribution to SAP BDC (zero-copy "
                       "share-back). Grants the listed tables to the share. Snowflake equivalent of "
                       "the Databricks Delta share create/update.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share to create/update"},
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fully-qualified tables/views to include (DB.SCHEMA.OBJECT)",
                },
                "ord_metadata": {"type": "object", "description": "ORD metadata (stored as share comment)"},
                "skip_validation": {"type": "boolean", "description": "Bypass ORD pre-flight (default false)"},
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "create_or_update_share_csn",
        "description": "Create or update a Snowflake share from CSN (Common Semantic Notation). Entities "
                       "in the CSN schema are resolved to Snowflake tables and granted to the share.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share"},
                "csn_schema": {"type": "object", "description": "CSN schema definition (definitions/entities)"},
                "database": {"type": "string", "description": "Snowflake database holding the CSN entities"},
                "schema": {"type": "string", "description": "Snowflake schema holding the CSN entities"},
            },
            "required": ["share_name", "csn_schema"],
        },
    },
    {
        "name": "publish_data_product",
        "description": "Publish a share to SAP BDC by associating it with the zero-copy connector "
                       "(ALTER ZEROCOPY CONNECTOR ... SET SHARE_BACK=TRUE; ADD SHARE). Snowflake "
                       "equivalent of publishing a Databricks data product.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share to publish"},
                "data_product_name": {"type": "string", "description": "Logical data product name (informational)"},
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "delete_share",
        "description": "Unpublish and delete a share: remove it from the zero-copy connector, then "
                       "DROP SHARE. Snowflake equivalent of delete_share.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share to delete"}
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "provision_share",
        "description": "End-to-end provisioning on Snowflake: create the share, grant the tables, and "
                       "(optionally) publish it to SAP BDC via the zero-copy connector — one operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {"type": "string", "description": "Name of the share to create"},
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fully-qualified tables to include (DB.SCHEMA.OBJECT)",
                },
                "ord_metadata": {"type": "object", "description": "ORD metadata (title/description/version)"},
                "comment": {"type": "string", "description": "Optional share comment"},
                "auto_publish": {"type": "boolean", "description": "Publish to the connector (default true)"},
                "skip_if_exists": {"type": "boolean", "description": "Skip share creation if it exists (default true)"},
            },
            "required": ["share_name", "tables"],
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _grant_tables_sql(share_name: str, tables: list[str]) -> list[str]:
    """Build GRANT statements to add tables (and their DB/schema usage) to a share."""
    stmts: list[str] = []
    dbs, schemas = set(), set()
    for t in tables:
        parts = t.split(".")
        if len(parts) == 3:
            dbs.add(parts[0])
            schemas.add(f"{parts[0]}.{parts[1]}")
    for db in sorted(dbs):
        stmts.append(f"GRANT USAGE ON DATABASE {db} TO SHARE {quote_ident(share_name)}")
    for sch in sorted(schemas):
        stmts.append(f"GRANT USAGE ON SCHEMA {sch} TO SHARE {quote_ident(share_name)}")
    for t in tables:
        stmts.append(f"GRANT SELECT ON TABLE {t} TO SHARE {quote_ident(share_name)}")
    return stmts


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def handle_list_shares(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    max_results = int(arguments.get("max_results", 100))
    try:
        rows = client.execute("SHOW SHARES")
    except SnowflakeError as e:
        return f"❌ Failed to list shares: {e}"
    out = []
    for r in rows[:max_results]:
        out.append({
            "name": r.get("name"),
            "kind": r.get("kind"),
            "database_name": r.get("database_name"),
            "to": r.get("to"),
            "owner": r.get("owner"),
            "comment": r.get("comment"),
        })
    return f"Found {len(out)} share(s):\n{json.dumps(out, indent=2, default=str)}"


def handle_get_share_details(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    try:
        objects = client.execute(f"DESC SHARE {quote_ident(share)}")
    except SnowflakeError as e:
        return f"❌ Failed to describe share '{share}': {e}"
    return (
        f"Share '{share}' — {len(objects)} granted object(s):\n"
        f"{json.dumps(objects, indent=2, default=str)}"
    )


def handle_list_recipients(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    result = {"connectors": [], "available_data_products": None}
    try:
        connectors = client.execute("SHOW ZEROCOPY CONNECTORS IN ACCOUNT")
        result["connectors"] = [
            {
                "name": c.get("name"),
                "partner": c.get("partner"),
                "status": c.get("status"),
                "share_back": c.get("share_back"),
                "database_name": c.get("database_name"),
            }
            for c in connectors
        ]
    except SnowflakeError as e:
        result["connectors_error"] = str(e)
    # Inbound SAP data products available through this connector.
    try:
        raw = client.execute_scalar(
            f"SELECT SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES({quote_literal(cfg.connector_name)})"
        )
        try:
            result["available_data_products"] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            result["available_data_products"] = raw
    except SnowflakeError as e:
        result["available_data_products_error"] = str(e)
    return json.dumps(result, indent=2, default=str)


def handle_create_or_update_share(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    tables = arguments.get("tables", []) or []
    ord_meta = arguments.get("ord_metadata")
    steps = []
    try:
        comment = ""
        if ord_meta:
            comment = f" COMMENT = {quote_literal(json.dumps(ord_meta)[:1000])}"
        client.execute(f"CREATE SHARE IF NOT EXISTS {quote_ident(share)}{comment}")
        steps.append(f"✓ share '{share}' created/exists")
        for stmt in _grant_tables_sql(share, tables):
            client.execute(stmt)
        if tables:
            steps.append(f"✓ granted {len(tables)} object(s) to the share")
    except SnowflakeError as e:
        steps.append(f"❌ {e}")
        return "Create/update share partially failed:\n" + "\n".join(steps)
    return f"Share '{share}' ready:\n" + "\n".join(steps)


def handle_create_or_update_share_csn(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    csn = arguments.get("csn_schema") or {}
    db = arguments.get("database")
    sch = arguments.get("schema")
    # Resolve CSN entities -> Snowflake tables.
    defs = csn.get("definitions", csn.get("entities", {})) if isinstance(csn, dict) else {}
    entity_names = list(defs.keys()) if isinstance(defs, dict) else []
    if not entity_names:
        return "❌ CSN schema has no 'definitions'/'entities' to resolve to tables."
    tables = []
    for name in entity_names:
        obj = name.split(".")[-1]
        if db and sch:
            tables.append(f"{db}.{sch}.{obj}")
        else:
            tables.append(obj)
    return handle_create_or_update_share(
        {"share_name": share, "tables": tables}, client, cfg
    ) + f"\n(resolved {len(tables)} CSN entities: {', '.join(entity_names)})"


def handle_publish_data_product(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    conn = cfg.connector_fqn
    steps = []
    try:
        client.execute(f"ALTER ZEROCOPY CONNECTOR {conn} SET SHARE_BACK = TRUE")
        steps.append("✓ SHARE_BACK enabled on connector")
        client.execute(f"ALTER ZEROCOPY CONNECTOR {conn} ADD SHARE {quote_ident(share)}")
        steps.append(f"✓ share '{share}' associated with connector '{cfg.connector_name}'")
    except SnowflakeError as e:
        steps.append(f"❌ {e}")
        return "Publish failed:\n" + "\n".join(steps)
    return (
        f"✅ Published share '{share}' to SAP BDC via connector '{cfg.connector_name}'.\n"
        + "\n".join(steps)
        + "\nThe data product should appear in the SAP BDC catalog shortly."
    )


def handle_delete_share(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    conn = cfg.connector_fqn
    steps = []
    try:
        client.execute(f"ALTER ZEROCOPY CONNECTOR {conn} REMOVE SHARE {quote_ident(share)}")
        steps.append(f"✓ removed '{share}' from connector")
    except SnowflakeError as e:
        steps.append(f"ℹ️  connector removal skipped: {e}")
    try:
        client.execute(f"DROP SHARE IF EXISTS {quote_ident(share)}")
        steps.append(f"✓ dropped share '{share}'")
    except SnowflakeError as e:
        steps.append(f"❌ {e}")
    return f"Delete share '{share}':\n" + "\n".join(steps)


def handle_provision_share(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    share = arguments["share_name"]
    tables = arguments.get("tables", []) or []
    ord_meta = arguments.get("ord_metadata")
    auto_publish = arguments.get("auto_publish", True)
    steps = [f"=== Provisioning share '{share}' ==="]

    create_out = handle_create_or_update_share(
        {"share_name": share, "tables": tables, "ord_metadata": ord_meta}, client, cfg
    )
    steps.append(create_out)

    if auto_publish:
        steps.append(handle_publish_data_product({"share_name": share}, client, cfg))
    else:
        steps.append("ℹ️  auto_publish=false — share created but not published to the connector.")
    return "\n".join(steps)


HANDLERS = {
    "list_shares": handle_list_shares,
    "get_share_details": handle_get_share_details,
    "list_recipients": handle_list_recipients,
    "create_or_update_share": handle_create_or_update_share,
    "create_or_update_share_csn": handle_create_or_update_share_csn,
    "publish_data_product": handle_publish_data_product,
    "delete_share": handle_delete_share,
    "provision_share": handle_provision_share,
}
