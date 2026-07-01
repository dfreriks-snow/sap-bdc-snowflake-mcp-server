"""Streamlit UI for the SAP BDC Snowflake MCP server.

This app is a real MCP *client*: it launches the sap-bdc-snowflake-mcp server as
a stdio subprocess, performs the MCP handshake, lists the 17 tools, and calls
them — rendering a dynamic form from each tool's input schema.

Run:
    pip install -e ".[ui]"      # or: pip install streamlit
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil

import pandas as pd
import streamlit as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

st.set_page_config(
    page_title="SAP BDC ↔ Snowflake Console",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Tools that mutate Snowflake / the connector — require explicit confirmation.
WRITE_TOOLS = {
    "create_or_update_share", "create_or_update_share_csn", "publish_data_product",
    "delete_share", "provision_share", "cleanup_orphaned_data_product",
}

# Per-tool glyphs for a friendlier tool list.
TOOL_ICONS = {
    "list_shares": "📤", "get_share_details": "🔎", "list_recipients": "📥",
    "create_or_update_share": "➕", "create_or_update_share_csn": "🧬",
    "publish_data_product": "🚀", "delete_share": "🗑️", "provision_share": "⚙️",
    "validate_tenant_hostname": "🌐", "validate_share_readiness": "🩺",
    "validate_snowflake_privileges": "🔐", "check_cld_asset_support": "🗄️",
    "list_unsupported_share_assets": "🚫", "validate_ord_metadata": "📋",
    "generate_csn_template": "🧾", "diagnose_share_error": "🧭",
    "cleanup_orphaned_data_product": "🧹",
}


def _tool_label(name: str) -> str:
    return f"{TOOL_ICONS.get(name, '•')}  {name}"


def inject_css() -> None:
    """Global styling: gradient hero, metric cards, buttons, sidebar."""
    st.markdown(
        """
        <style>
          .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1400px; }

          .bdc-hero {
            background: linear-gradient(120deg, #0A6ED1 0%, #1E9BE0 55%, #29B5E8 100%);
            border-radius: 18px; padding: 24px 30px; margin-bottom: 20px;
            color: #ffffff; box-shadow: 0 10px 28px rgba(10,110,209,0.28);
          }
          .bdc-hero h1 { font-size: 1.65rem; margin: 0 0 6px 0; color: #fff; font-weight: 750; letter-spacing:.2px; }
          .bdc-hero p  { margin: 0; opacity: .93; font-size: .97rem; max-width: 900px; }
          .bdc-badges  { margin-top: 14px; }
          .bdc-badge {
            display: inline-block; background: rgba(255,255,255,0.16);
            border: 1px solid rgba(255,255,255,0.40); color: #fff;
            padding: 4px 12px; border-radius: 999px; font-size: .82rem;
            margin-right: 8px; margin-top: 4px; font-weight: 600;
          }

          [data-testid="stMetric"] {
            background: #ffffff; border: 1px solid #e6e9ef; border-radius: 14px;
            padding: 16px 18px; box-shadow: 0 2px 10px rgba(16,30,54,0.06);
            transition: transform .12s ease, box-shadow .12s ease;
          }
          [data-testid="stMetric"]:hover {
            transform: translateY(-2px); box-shadow: 0 8px 20px rgba(16,30,54,0.10);
          }
          [data-testid="stMetricLabel"] p { font-size: .78rem; color: #5b6472; font-weight: 600; }
          [data-testid="stMetricValue"] { color: #0A6ED1; font-weight: 750; }

          .stButton > button {
            border-radius: 10px; font-weight: 600; border: 1px solid #d8dee9;
          }
          .stButton > button:hover { border-color: #0A6ED1; color: #0A6ED1; }
          .stButton > button[kind="primary"] {
            background: linear-gradient(120deg, #0A6ED1, #1E9BE0);
            border: none; color: #fff;
          }

          section[data-testid="stSidebar"] { background: #f6f9fd; border-right: 1px solid #e6e9ef; }
          .bdc-section { font-size: 1.02rem; font-weight: 700; color: #1c2b45; margin: 2px 0 8px 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def hero(conn: str, connector: str, n_tools: int) -> None:
    st.markdown(
        f"""
        <div class="bdc-hero">
          <h1>🔗 SAP BDC&nbsp;↔&nbsp;Snowflake Connector Console</h1>
          <p>Operate the SAP BDC Connect zero-copy connector — browse inbound data products and
             catalog-linked databases, publish shares back to SAP BDC, and validate readiness —
             all through the MCP toolset.</p>
          <div class="bdc-badges">
            <span class="bdc-badge">🧰 {n_tools} tools</span>
            <span class="bdc-badge">❄️ {conn}</span>
            <span class="bdc-badge">🔌 {connector}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )



def _server_params() -> StdioServerParameters:
    """Build stdio params to launch the MCP server with the chosen env."""
    env = dict(os.environ)
    env["SNOWFLAKE_CONNECTION"] = st.session_state.get("sf_conn", "dfreriksdemo")
    env["BDC_CONNECTOR_NAME"] = st.session_state.get("connector", "SAP_BDC_CONNECT_ZC")
    env["BDC_CONNECTOR_DATABASE"] = st.session_state.get("conn_db", "SAP_BDC_CONNECT")
    env["BDC_CONNECTOR_SCHEMA"] = st.session_state.get("conn_schema", "PUBLIC")

    exe = shutil.which("sap-bdc-snowflake-mcp")
    if exe:
        return StdioServerParameters(command=exe, args=[], env=env)
    # Fallback: run the module with the current interpreter.
    import sys
    return StdioServerParameters(
        command=sys.executable, args=["-m", "sap_bdc_snowflake_mcp.server"], env=env
    )


async def _mcp(action: str, tool: str | None = None, args: dict | None = None):
    """Connect to the server, run one action (list tools or call a tool), disconnect."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if action == "list":
                resp = await session.list_tools()
                return [
                    {"name": t.name, "description": t.description,
                     "schema": t.inputSchema or {}}
                    for t in resp.tools
                ]
            result = await session.call_tool(tool, args or {})
            parts = []
            for c in result.content:
                parts.append(getattr(c, "text", str(c)))
            return "\n".join(parts)


def run_mcp(action: str, tool: str | None = None, args: dict | None = None):
    return asyncio.run(_mcp(action, tool, args))


@st.cache_data(show_spinner="Connecting to the MCP server…")
def load_tools(conn: str, connector: str):
    # conn/connector in the cache key so tools reload if the target changes.
    return run_mcp("list")


@st.cache_data(show_spinner="Loading catalog-linked databases…")
def load_clds(conn: str, connector: str) -> list[str]:
    """Call check_cld_asset_support in discovery mode; return existing CLD names."""
    try:
        out = run_mcp("call", "check_cld_asset_support", {})
        start = out.find("{")
        if start < 0:
            return []
        data = json.loads(out[start:])
        return [c["name"] for c in data.get("catalog_linked_databases", []) if c.get("name")]
    except Exception:  # noqa: BLE001
        return []


@st.cache_data(show_spinner="Loading schemas…")
def load_schemas(conn: str, database: str) -> list[str]:
    """List schemas in a database (excluding internal schemas) for the schema dropdown."""
    if not database:
        return []
    try:
        from sap_bdc_snowflake_mcp.config import BDCConfig
        from sap_bdc_snowflake_mcp.snowflake_client import SnowflakeClient, quote_ident

        client = SnowflakeClient(BDCConfig(connection_name=conn))
        rows = client.execute(f"SHOW SCHEMAS IN DATABASE {quote_ident(database)}")
        out = []
        for r in rows:
            n = r.get("name")
            if not n:
                continue
            u = str(n).upper()
            if u == "INFORMATION_SCHEMA" or u.endswith("$"):  # skip internal schemas
                continue
            out.append(n)
        return out
    except Exception:  # noqa: BLE001
        return []


@st.cache_data(show_spinner="Computing connector KPIs…")
def load_kpis(conn: str, connector: str, conn_db: str, conn_schema: str) -> dict | None:
    """Connector-level KPIs: inbound CLDs, share-back shares, and columns shared from BDC."""
    try:
        from sap_bdc_snowflake_mcp.config import BDCConfig
        from sap_bdc_snowflake_mcp.snowflake_client import SnowflakeClient, quote_ident

        cfg = BDCConfig(
            connection_name=conn, connector_name=connector,
            connector_database=conn_db, connector_schema=conn_schema,
        )
        client = SnowflakeClient(cfg)
        desc = client.execute(f"DESCRIBE ZEROCOPY CONNECTOR {cfg.connector_fqn}")[0]
        clds = [x.strip() for x in (desc.get("catalog_linked_databases") or "").split(",") if x.strip()]
        shares = [x.strip() for x in (desc.get("shares") or "").split(",") if x.strip()]

        # Data products available from SAP BDC (inbound), via the connector's share list.
        data_products = 0
        try:
            raw = client.execute_scalar(
                f"SELECT SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES('{cfg.connector_fqn}')"
            )
            data_products = len(json.loads(raw)) if raw else 0
        except Exception:  # noqa: BLE001
            data_products = 0

        columns = 0
        for db in clds:
            try:
                rows = client.execute(f"SHOW COLUMNS IN DATABASE {quote_ident(db)}")
            except Exception:  # noqa: BLE001
                continue
            for r in rows:
                s = str(r.get("schema_name") or "").upper()
                if s == "INFORMATION_SCHEMA" or s.endswith("$"):  # skip internal schemas
                    continue
                columns += 1
        return {
            "data_products": data_products,
            "clds": len(clds),
            "shares_back": len(shares),
            "columns": columns,
        }
    except Exception:  # noqa: BLE001
        return None


# Friendly labels for SAP BDC business-function namespace segments.
BDC_FUNCTIONS = {
    "workforce": "Workforce", "analytics": "Analytics",
    "foundationobjects": "Foundation Objects", "learning": "Learning",
    "bdcconnect": "BDC Connect", "recruiting": "Recruiting",
    "compensation": "Compensation", "finance": "Finance",
}


def _bdc_function(namespace: str) -> str:
    """Map an SAP ORD namespace (e.g. sap.bdc.sf.workforce) to a business function."""
    seg = namespace.split(".")[-1] if namespace else ""
    return BDC_FUNCTIONS.get(seg, seg.replace("_", " ").title() if seg else "Unknown")


@st.cache_data(show_spinner="Reading SAP BDC system…")
def load_bdc_system(conn: str, connector: str, conn_db: str, conn_schema: str) -> dict | None:
    """Describe the connected SAP BDC system and its available data products by function."""
    try:
        from sap_bdc_snowflake_mcp.config import BDCConfig
        from sap_bdc_snowflake_mcp.snowflake_client import SnowflakeClient, quote_ident

        cfg = BDCConfig(
            connection_name=conn, connector_name=connector,
            connector_database=conn_db, connector_schema=conn_schema,
        )
        client = SnowflakeClient(cfg)
        desc = client.execute(f"DESCRIBE ZEROCOPY CONNECTOR {cfg.connector_fqn}")[0]
        try:
            endpoint = json.loads(desc.get("config") or "{}").get("sap_bdc_connector_endpoint", "")
        except json.JSONDecodeError:
            endpoint = ""
        host = endpoint.replace("https://", "").replace("http://", "").split("/")[0]

        raw = client.execute_scalar(
            f"SELECT SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES('{cfg.connector_fqn}')"
        )
        products = []
        for e in (json.loads(raw) if raw else []):
            props = e.get("properties", {}) or {}
            ordid = props.get("sap.ord.apiResource.ordId", "")
            ns = ordid.split(":")[0] if ordid else ""
            if not ns:
                parts = (e.get("name") or "").split(":")
                ns = parts[parts.index("ns") + 1] if "ns" in parts else ""
            display = (e.get("display_name") or e.get("name") or "").split(" (")[0].strip()
            linked = [c.get("name") for c in (e.get("catalog_linked_databases") or []) if c.get("name")]
            products.append({
                "Business function": _bdc_function(ns),
                "Data product": display,
                "Source system": props.get("sap.ord.systemInstance.name", ""),
                "Status": e.get("status", ""),
                "Linked database": linked[0] if linked else "",
                "_share": f"shares/{e.get('name')}" if e.get("name") else "",
                "_resource": (e.get("name") or "").split(":r:")[-1].split(":v:")[0] if ":r:" in (e.get("name") or "") else display,
            })

        # Derive the connector's internal catalog id from any existing CLD (needed to mount more).
        catalog_id = ""
        mounted = [p["Linked database"] for p in products if p["Linked database"]]
        if mounted:
            try:
                ddl = client.execute_scalar(f"SELECT GET_DDL('database', {quote_ident(mounted[0])})")
                m = re.search(r"catalog = '(ZEROCOPY\$[0-9A-Fa-f]+)'", ddl or "")
                catalog_id = m.group(1) if m else ""
            except Exception:  # noqa: BLE001
                catalog_id = ""

        return {
            "partner": desc.get("partner", ""),
            "status": desc.get("status", ""),
            "host": host,
            "endpoint": endpoint,
            "products": products,
            "catalog_id": catalog_id,
            "systems": sorted({p["Source system"] for p in products if p["Source system"]}),
        }
    except Exception:  # noqa: BLE001
        return None


def mount_data_product(conn: str, connector: str, conn_db: str, conn_schema: str,
                       db_name: str, catalog_id: str, share: str) -> str:
    """Create a catalog-linked database for an available SAP BDC data product."""
    from sap_bdc_snowflake_mcp.config import BDCConfig
    from sap_bdc_snowflake_mcp.snowflake_client import SnowflakeClient, quote_ident, quote_literal

    cfg = BDCConfig(
        connection_name=conn, connector_name=connector,
        connector_database=conn_db, connector_schema=conn_schema,
    )
    client = SnowflakeClient(cfg)
    sql = (
        f"CREATE DATABASE {quote_ident(db_name)} LINKED_CATALOG = ("
        f"catalog = {quote_literal(catalog_id)} "
        f"catalog_name = {quote_literal(share)} "
        f"namespace_mode = IGNORE_NESTED_NAMESPACE "
        f"sync_interval_seconds = 86400 "
        f"allowed_write_operations = NONE)"
    )
    client.execute(sql)
    return db_name


def render_form(schema: dict, key_prefix: str) -> dict:
    """Render inputs from a JSON Schema; return the collected arguments dict."""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    args: dict = {}
    if not props:
        st.caption("This tool takes no arguments.")
        return args
    for name, spec in props.items():
        typ = spec.get("type", "string")
        desc = spec.get("description", "")
        label = f"{name}{' *' if name in required else ''}"
        wkey = f"{key_prefix}:{name}"

        # For the CLD tool, pre-populate `database` with existing catalog-linked
        # databases discovered on the account (blank = list all CLDs).
        if key_prefix == "check_cld_asset_support" and name == "database":
            clds = load_clds(st.session_state.sf_conn, st.session_state.connector)
            if clds:
                st.caption(f"{len(clds)} catalog-linked database(s) found on this account.")
            choice = st.selectbox(
                label, options=clds, index=None, key=wkey,
                placeholder="Leave blank to list all CLDs, or pick one to scan",
                help=desc, accept_new_options=True,
            )
            if choice and str(choice).strip():
                args[name] = str(choice).strip()
            continue

        # Once a database is chosen, offer its schemas as a dropdown.
        if key_prefix == "check_cld_asset_support" and name == "schema":
            db_choice = args.get("database")
            if not db_choice:
                st.caption("Select a database above to choose a schema (optional).")
                continue
            schemas = load_schemas(st.session_state.sf_conn, db_choice)
            choice = st.selectbox(
                label, options=schemas, index=None,
                key=f"{wkey}:{db_choice}",  # reset when the database changes
                placeholder="All schemas (leave blank), or pick one",
                help=desc, accept_new_options=True,
            )
            if choice and str(choice).strip():
                args[name] = str(choice).strip()
            continue

        if typ == "boolean":
            args[name] = st.checkbox(label, key=wkey, help=desc)
        elif typ == "integer":
            val = st.text_input(label, key=wkey, help=desc, placeholder="integer")
            if val.strip():
                try:
                    args[name] = int(val)
                except ValueError:
                    st.warning(f"{name}: expected an integer")
        elif typ in ("object", "array"):
            raw = st.text_area(label + "  (JSON)", key=wkey, help=desc, height=120,
                               placeholder='{ }' if typ == "object" else "[ ]")
            if raw.strip():
                try:
                    args[name] = json.loads(raw)
                except json.JSONDecodeError as e:
                    st.warning(f"{name}: invalid JSON — {e}")
        else:  # string
            if name in ("csn_schema", "ord", "ord_metadata"):
                raw = st.text_area(label, key=wkey, help=desc, height=100)
            else:
                raw = st.text_input(label, key=wkey, help=desc)
            if raw.strip():
                args[name] = raw
    return args


def _json_start(text: str) -> int:
    """Index of the first JSON object/array in a tool's text output, else -1."""
    candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    return min(candidates) if candidates else -1


def _render_value(key: str, value) -> None:
    """Render one non-scalar top-level field (list or nested dict)."""
    if isinstance(value, list):
        st.markdown(f"**{key}** ({len(value)})")
        if not value:
            st.caption("— none —")
        elif all(isinstance(x, dict) for x in value):
            try:
                st.dataframe(value, use_container_width=True, hide_index=True)
            except Exception:  # noqa: BLE001
                st.json(value)
        else:
            for item in value:
                st.markdown(f"- {item}")
    elif isinstance(value, dict):
        st.markdown(f"**{key}**")
        st.json(value)


def render_result(out: str | None) -> None:
    """Pretty-print a tool result: status banner + summary metrics + tables."""
    if out is None:
        return
    st.markdown("### Result")

    idx = _json_start(out)
    preamble = (out[:idx] if idx > 0 else (out if idx < 0 else "")).strip()
    payload = out[idx:].strip() if idx >= 0 else ""

    if preamble:
        head = preamble.splitlines()[0]
        if head.startswith("❌") or "FAILED" in head.upper():
            st.error(preamble)
        elif head.startswith("⚠️") or "WARN" in head.upper():
            st.warning(preamble)
        elif head.startswith("ℹ️"):
            st.info(preamble)
        elif head.startswith("✅"):
            st.success(preamble)
        else:
            st.write(preamble)

    data = None
    if payload:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None

    if data is None:
        if not preamble:  # nothing structured — show the raw text
            st.code(out)
        return

    if isinstance(data, list):
        _render_value("items", data)
        with st.expander("Raw JSON"):
            st.json(data)
        return

    if not isinstance(data, dict):
        st.write(data)
        return

    scalars = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
    collections = {k: v for k, v in data.items() if isinstance(v, (list, dict))}

    if scalars:
        st.markdown("**Summary**")
        st.table({
            "field": list(scalars.keys()),
            "value": ["" if v is None else str(v) for v in scalars.values()],
        })

    for k, v in collections.items():
        _render_value(k, v)

    with st.expander("Raw JSON"):
        st.json(data)


def main() -> None:
    inject_css()

    with st.sidebar:
        st.markdown("### ❄️  Connection")
        st.caption("Target Snowflake account & SAP BDC connector")
        st.session_state.sf_conn = st.text_input("Snowflake connection", value=st.session_state.get("sf_conn", "dfreriksdemo"))
        st.session_state.connector = st.text_input("Connector", value=st.session_state.get("connector", "SAP_BDC_CONNECT_ZC"))
        st.session_state.conn_db = st.text_input("Connector DB", value=st.session_state.get("conn_db", "SAP_BDC_CONNECT"))
        st.session_state.conn_schema = st.text_input("Connector schema", value=st.session_state.get("conn_schema", "PUBLIC"))
        st.session_state.bdc_ui_url = st.text_input(
            "SAP BDC UI URL",
            value=st.session_state.get(
                "bdc_ui_url",
                "https://snowflake-eng-tdd.us10.hcs.cloud.sap/bdc-ui/index.html#/bdc_home",
            ),
            help="Link to the SAP BDC catalog UI where the full set of data products is browsed and subscribed.",
        )
        st.divider()
        if st.button("🔄  Reconnect / reload", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Powered by the SAP BDC Snowflake MCP server.")

    try:
        tools = load_tools(st.session_state.sf_conn, st.session_state.connector)
    except Exception as exc:  # noqa: BLE001
        inject_css()
        st.error(f"Could not start / connect to the MCP server:\n\n{exc}")
        st.stop()

    hero(st.session_state.sf_conn, st.session_state.connector, len(tools))

    kpis = load_kpis(
        st.session_state.sf_conn, st.session_state.connector,
        st.session_state.conn_db, st.session_state.conn_schema,
    )
    if kpis:
        st.markdown('<div class="bdc-section">📊 Connector overview</div>', unsafe_allow_html=True)
        k0, k1, k2, k3 = st.columns(4)
        k0.metric("📦 Data products from SAP BDC", kpis["data_products"],
                  help="Inbound SAP BDC data products visible to the connector "
                       "(SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES).")
        k1.metric("🗄️ CLDs shared from BDC", kpis["clds"],
                  help="Catalog-linked databases consumed inbound from SAP BDC.")
        k2.metric("📤 Shares published back", kpis["shares_back"],
                  help="Snowflake shares associated with the connector (share_back).")
        k3.metric("🔢 Columns shared from BDC", f"{kpis['columns']:,}",
                  help="Total data columns across the inbound catalog-linked databases "
                       "(excludes INFORMATION_SCHEMA and internal schemas).")
    st.divider()

    system = load_bdc_system(
        st.session_state.sf_conn, st.session_state.connector,
        st.session_state.conn_db, st.session_state.conn_schema,
    )

    # Sidebar: list the connected entities (Snowflake + SAP BDC system).
    with st.sidebar:
        st.divider()
        st.markdown("### 🔗 Connected entities")
        st.markdown(f"**❄️ Snowflake account**  \n`{st.session_state.sf_conn}`")
        st.markdown(f"**🔌 Zero-copy connector**  \n`{st.session_state.connector}`  ·  {len(tools)} tools")
        if system:
            dot = "🟢" if str(system["status"]).upper() == "CONNECTED" else "🔴"
            st.markdown(
                f"**🏢 SAP BDC system** {dot} {system['status']}  \n"
                f"Partner: `{system['partner']}`  \n"
                f"Host: `{system['host'] or '—'}`  \n"
                f"{len(system['products'])} data products · {len(system['systems'])} source system(s)"
            )
        else:
            st.caption("🏢 SAP BDC system — unavailable")

    if system:
        st.markdown('<div class="bdc-section">🏢 SAP BDC system</div>', unsafe_allow_html=True)
        products = system["products"]
        status_ok = str(system["status"]).upper() == "CONNECTED"
        st.markdown(
            f'<span class="bdc-badge" style="background:#eef4ff;border-color:#c9dcff;color:#0A6ED1;">'
            f'{"🟢" if status_ok else "🔴"} {system["status"] or "UNKNOWN"}</span>'
            f'<span class="bdc-badge" style="background:#eef4ff;border-color:#c9dcff;color:#0A6ED1;">'
            f'🤝 {system["partner"]}</span>'
            f'<span class="bdc-badge" style="background:#eef4ff;border-color:#c9dcff;color:#0A6ED1;">'
            f'🌐 {system["host"] or "—"}</span>'
            f'<span class="bdc-badge" style="background:#eef4ff;border-color:#c9dcff;color:#0A6ED1;">'
            f'🖥️ {len(system["systems"])} source system(s)</span>',
            unsafe_allow_html=True,
        )
        st.caption(f"{len(products)} data products available from SAP BDC"
                   + (f" · source systems: {', '.join(system['systems'])}" if system["systems"] else ""))

        link_col, note_col = st.columns([1, 3])
        with link_col:
            if st.session_state.get("bdc_ui_url"):
                st.link_button("🗂️ Browse full catalog in SAP BDC ↗",
                               st.session_state.bdc_ui_url, use_container_width=True)
        with note_col:
            st.info(
                "Snowflake only sees data products that SAP BDC has **shared to this connector** "
                f"({len(products)} shown here). The full SAP BDC catalog (e.g. hundreds of products) "
                "is browsed and **subscribed in the SAP BDC UI** — newly subscribed products then "
                "appear here automatically and can be mounted below.",
                icon="ℹ️",
            )

        if products:
            display_cols = ["Business function", "Data product", "Source system", "Status", "Linked database"]
            by_fn = (
                pd.DataFrame(products)
                .groupby("Business function").size()
                .reset_index(name="Data products")
                .sort_values("Data products", ascending=False)
            )
            c_left, c_right = st.columns([1, 1], gap="large")
            with c_left:
                st.markdown("**Available data products by SAP business function**")
                st.bar_chart(by_fn.set_index("Business function"), height=260, color="#0A6ED1")
            with c_right:
                st.markdown("**Counts**")
                st.dataframe(by_fn, use_container_width=True, hide_index=True)
            with st.expander(f"📦 All available data products ({len(products)})"):
                st.dataframe(pd.DataFrame(products)[display_cols],
                             use_container_width=True, hide_index=True)

            # Consume: mount data products that are shared to the connector but not yet linked.
            st.markdown("**🔗 Consume data products → catalog-linked databases**")
            unmounted = [p for p in products if not p["Linked database"]]
            if not unmounted:
                st.caption("✅ All data products shared to this connector are already mounted "
                           "as catalog-linked databases.")
            elif not system["catalog_id"]:
                st.caption("⚠️ Cannot mount: no existing catalog-linked database to derive the "
                           "connector catalog id from. Mount one product via SQL first.")
            else:
                labels = {f"{p['Data product']}  ·  {p['Business function']}": p for p in unmounted}
                pick = st.selectbox("Data product to mount", options=list(labels),
                                    index=None, placeholder="Select an unmounted data product")
                if pick:
                    prod = labels[pick]
                    default_name = re.sub(r"[^0-9A-Za-z]+", "_", prod["_resource"]).strip("_").upper() + "_V1"
                    db_name = st.text_input("New catalog-linked database name", value=default_name)
                    st.warning("⚠️ This creates a new catalog-linked database in Snowflake.")
                    ok = st.checkbox("Yes, create this catalog-linked database")
                    if st.button("🚀 Mount data product", type="primary", disabled=not (ok and db_name.strip())):
                        with st.spinner(f"Creating {db_name}…"):
                            try:
                                mount_data_product(
                                    st.session_state.sf_conn, st.session_state.connector,
                                    st.session_state.conn_db, st.session_state.conn_schema,
                                    db_name.strip(), system["catalog_id"], prod["_share"],
                                )
                                st.success(f"✅ Mounted `{prod['Data product']}` as catalog-linked "
                                           f"database `{db_name.strip()}`.")
                                st.cache_data.clear()
                            except Exception as exc:  # noqa: BLE001
                                st.error(f"Mount failed: {exc}")
        st.divider()

    by_name = {t["name"]: t for t in tools}
    names = sorted(by_name)

    left, right = st.columns([1, 2], gap="large")

    with left:
        st.markdown('<div class="bdc-section">🧰 Tools</div>', unsafe_allow_html=True)
        # Keep the selection valid and bind the radio directly to this key so
        # both the quick-action buttons and the radio drive the same state.
        if st.session_state.get("selected_tool") not in names:
            st.session_state["selected_tool"] = names[0]

        st.caption("⚡ Quick actions (read-only)")
        for quick in ("list_recipients", "list_shares", "validate_snowflake_privileges"):
            if quick in by_name and st.button(_tool_label(quick), key=f"q:{quick}", use_container_width=True):
                st.session_state["selected_tool"] = quick
                st.session_state["autorun"] = quick
                st.rerun()
        st.divider()
        st.radio("All tools", names, key="selected_tool", format_func=_tool_label)
        selected = st.session_state["selected_tool"]

    with right:
        tool = by_name[selected]
        is_write = selected in WRITE_TOOLS

        with st.container(border=True):
            badge = "✏️ write" if is_write else "👁️ read-only"
            st.markdown(f"### {TOOL_ICONS.get(selected, '•')}  {selected}  &nbsp;`{badge}`")
            st.caption(tool["description"])

            args = render_form(tool["schema"], key_prefix=selected)

            confirmed = True
            if is_write:
                st.warning("⚠️ This tool modifies Snowflake / the connector.")
                confirmed = st.checkbox("Yes, run this write operation", key=f"confirm:{selected}")

            autorun = st.session_state.pop("autorun", None) == selected
            run = st.button("▶  Run tool", type="primary", use_container_width=True,
                            disabled=is_write and not confirmed)

        if run or (autorun and not is_write):
            with st.spinner(f"Calling {selected}…"):
                try:
                    out = run_mcp("call", selected, args)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Call failed: {exc}")
                    out = None
            if out is not None:
                render_result(out)


if __name__ == "__main__":
    main()
