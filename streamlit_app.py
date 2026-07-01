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
import shutil

import streamlit as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

st.set_page_config(page_title="SAP BDC Snowflake MCP", page_icon="🔗", layout="wide")

# Tools that mutate Snowflake / the connector — require explicit confirmation.
WRITE_TOOLS = {
    "create_or_update_share", "create_or_update_share_csn", "publish_data_product",
    "delete_share", "provision_share", "cleanup_orphaned_data_product",
}


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


def main() -> None:
    st.title("🔗 SAP BDC Snowflake MCP")
    st.caption("A Streamlit MCP client for the SAP BDC Connect zero-copy connector tools.")

    with st.sidebar:
        st.subheader("Target")
        st.session_state.sf_conn = st.text_input("Snowflake connection", value=st.session_state.get("sf_conn", "dfreriksdemo"))
        st.session_state.connector = st.text_input("Connector", value=st.session_state.get("connector", "SAP_BDC_CONNECT_ZC"))
        st.session_state.conn_db = st.text_input("Connector DB", value=st.session_state.get("conn_db", "SAP_BDC_CONNECT"))
        st.session_state.conn_schema = st.text_input("Connector schema", value=st.session_state.get("conn_schema", "PUBLIC"))
        if st.button("🔄 Reconnect / reload tools", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    try:
        tools = load_tools(st.session_state.sf_conn, st.session_state.connector)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not start / connect to the MCP server:\n\n{exc}")
        st.stop()

    st.success(f"Connected — {len(tools)} tools available on connector "
               f"`{st.session_state.connector}`.")

    by_name = {t["name"]: t for t in tools}
    names = sorted(by_name)

    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("Tools")
        # Quick read-only actions
        st.caption("Quick actions (read-only)")
        for quick in ("list_recipients", "list_shares", "validate_snowflake_privileges"):
            if quick in by_name and st.button(f"▶ {quick}", key=f"q:{quick}", use_container_width=True):
                st.session_state["selected_tool"] = quick
                st.session_state["autorun"] = quick
        st.divider()
        selected = st.radio(
            "All tools", names,
            index=names.index(st.session_state.get("selected_tool", names[0]))
            if st.session_state.get("selected_tool") in names else 0,
            key="tool_radio",
        )
        st.session_state["selected_tool"] = selected

    with right:
        tool = by_name[selected]
        st.subheader(selected)
        st.caption(tool["description"])

        is_write = selected in WRITE_TOOLS
        args = render_form(tool["schema"], key_prefix=selected)

        confirmed = True
        if is_write:
            st.warning("⚠️ This tool modifies Snowflake / the connector.")
            confirmed = st.checkbox("Yes, I want to run this write operation", key=f"confirm:{selected}")

        autorun = st.session_state.pop("autorun", None) == selected
        run = st.button("Run tool", type="primary", disabled=is_write and not confirmed)

        if run or (autorun and not is_write):
            with st.spinner(f"Calling {selected}…"):
                try:
                    out = run_mcp("call", selected, args)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Call failed: {exc}")
                    out = None
            if out is not None:
                st.markdown("**Result**")
                try:
                    st.json(json.loads(out))
                except (json.JSONDecodeError, TypeError):
                    st.code(out)


if __name__ == "__main__":
    main()
