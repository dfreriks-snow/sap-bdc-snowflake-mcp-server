# Changelog

## 0.1.0 — Snowflake port

Initial release: Snowflake port of the Databricks `sap-bdc-mcp-server`
(Mario DeFelipe, MIT).

- Replaced `databricks-sdk` / `sap-bdc-connect-sdk` with `snowflake-connector-python`.
- Auth via named Snowflake connection (`connections.toml`) or `SNOWFLAKE_*` env vars.
- Remapped all 17 tools to the SAP BDC Connect **zero-copy connector** model
  (`ZEROCOPY CONNECTOR`, Snowflake shares, `SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES`,
  catalog-linked databases). See `docs/CONVERSION.md`.
- Renamed `validate_databricks_privileges` → `validate_snowflake_privileges` and
  `check_deletion_vectors` → `check_cld_asset_support`.
- Ported platform-neutral logic verbatim: `validate_tenant_hostname`,
  `validate_ord_metadata`, and the `diagnose_share_error` SAP-Note rule table
  (plus new Snowflake connector-state guidance).
- Packaged as a stdio MCP server for Cortex Code (`sap-bdc-snowflake-mcp`).
