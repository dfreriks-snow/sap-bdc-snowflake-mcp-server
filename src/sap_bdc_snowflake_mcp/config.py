"""Configuration for the SAP BDC Snowflake MCP Server.

Ported from the Databricks-based sap-bdc-mcp-server (Mario DeFelipe, MIT).
Instead of a Databricks workspace host/token + Delta Sharing recipient, this
server talks to Snowflake and drives the SAP BDC Connect **zero-copy connector**.

Connection resolution (in order):
  1. SNOWFLAKE_CONNECTION -> a named connection in ~/.snowflake/connections.toml
  2. discrete SNOWFLAKE_* env vars (account/user/authenticator/password/etc.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class BDCConfig:
    """Configuration for the Snowflake SAP BDC Connect MCP server."""

    # --- Snowflake connection -------------------------------------------------
    connection_name: Optional[str] = None          # named connection in connections.toml
    account: Optional[str] = None
    user: Optional[str] = None
    role: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None

    # --- SAP BDC zero-copy connector ------------------------------------------
    connector_name: str = "SAP_BDC_CONNECT_ZC"
    connector_database: str = "SAP_BDC_CONNECT"
    connector_schema: str = "PUBLIC"

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "BDCConfig":
        """Load configuration from environment variables."""
        return cls(
            connection_name=os.getenv("SNOWFLAKE_CONNECTION"),
            account=os.getenv("SNOWFLAKE_ACCOUNT"),
            user=os.getenv("SNOWFLAKE_USER"),
            role=os.getenv("SNOWFLAKE_ROLE"),
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
            database=os.getenv("SNOWFLAKE_DATABASE"),
            schema=os.getenv("SNOWFLAKE_SCHEMA"),
            connector_name=os.getenv("BDC_CONNECTOR_NAME", "SAP_BDC_CONNECT_ZC"),
            connector_database=os.getenv("BDC_CONNECTOR_DATABASE", "SAP_BDC_CONNECT"),
            connector_schema=os.getenv("BDC_CONNECTOR_SCHEMA", "PUBLIC"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    @property
    def connector_fqn(self) -> str:
        """Fully qualified connector name: DB.SCHEMA.CONNECTOR."""
        return f"{self.connector_database}.{self.connector_schema}.{self.connector_name}"

    def to_dict(self) -> dict:
        return {
            "connection_name": self.connection_name,
            "account": self.account,
            "connector_name": self.connector_name,
            "connector_database": self.connector_database,
            "connector_schema": self.connector_schema,
            "log_level": self.log_level,
        }
