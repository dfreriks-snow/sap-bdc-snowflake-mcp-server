"""SAP BDC Snowflake MCP Server — stdio entry point.

A Model Context Protocol server that manages SAP Business Data Cloud (BDC)
integration on **Snowflake** via the SAP BDC Connect zero-copy connector.

This is a port of the Databricks-based sap-bdc-mcp-server (Mario DeFelipe, MIT)
to Snowflake technologies + Cortex Code. The 20 tools are aggregated from four
modules; each handler has signature ``handle_x(arguments, client, cfg) -> str``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import connector_tools, diagnostics_tools, metadata_tools, validation_tools
from .config import BDCConfig
from .snowflake_client import SnowflakeClient, SnowflakeError

# Load .env from the working directory / package dir if present.
load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("sap-bdc-snowflake-mcp")

app = Server("sap-bdc-snowflake-mcp")

# Aggregate schemas + handlers from all tool modules.
_MODULES = (connector_tools, validation_tools, metadata_tools, diagnostics_tools)
ALL_SCHEMAS: list[dict] = []
HANDLERS: dict = {}
for _m in _MODULES:
    ALL_SCHEMAS.extend(_m.SCHEMAS)
    HANDLERS.update(_m.HANDLERS)

# Lazily-initialised shared state.
_cfg: BDCConfig | None = None
_client: SnowflakeClient | None = None


def _get_context() -> tuple[SnowflakeClient, BDCConfig]:
    global _cfg, _client
    if _cfg is None:
        _cfg = BDCConfig.from_env()
    if _client is None:
        _client = SnowflakeClient(_cfg)
    return _client, _cfg


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return all 20 SAP BDC (Snowflake) tools."""
    return [Tool(**schema) for schema in ALL_SCHEMAS]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to its handler."""
    handler = HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]
    try:
        client, cfg = _get_context()
        result = handler(arguments or {}, client, cfg)
    except SnowflakeError as exc:
        result = f"❌ Snowflake error: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface any handler error to the caller
        logger.exception("Tool %s failed", name)
        result = f"❌ Tool '{name}' failed: {exc}"
    return [TextContent(type="text", text=str(result))]


async def main() -> None:
    logger.info("Starting SAP BDC Snowflake MCP Server (%d tools)...", len(ALL_SCHEMAS))
    cfg = BDCConfig.from_env()
    logger.info("Connector: %s | connection: %s",
                cfg.connector_fqn, cfg.connection_name or "(discrete env)")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run() -> None:
    """Synchronous entry point for the console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
