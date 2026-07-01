# Conversion: Databricks → Snowflake

This project ports [`sap-bdc-mcp-server`](https://github.com/MarioDeFelipe/sap-bdc-mcp-server)
(Databricks) to Snowflake. The tool surface is preserved (17 tools); the
implementation is remapped from Databricks Delta Sharing to the **SAP BDC Connect
zero-copy connector**.

## Platform model

| Concept | Databricks (original) | Snowflake (this port) |
|---------|-----------------------|-----------------------|
| Client SDK | `databricks-sdk`, `sap-bdc-connect-sdk` | `snowflake-connector-python` |
| Auth | workspace host + PAT (`DATABRICKS_HOST/TOKEN`) | named connection in `connections.toml` (`SNOWFLAKE_CONNECTION`) or `SNOWFLAKE_*` |
| Sharing primitive | Delta Sharing share + recipient | Snowflake `SHARE` + zero-copy connector (`PARTNER = SAP_BDC`) |
| Publish | grant share to recipient | `ALTER ZEROCOPY CONNECTOR … SET SHARE_BACK=TRUE; ADD SHARE` |
| Consume | recipient mounts share | catalog-linked database from connector |
| Discover inbound | Delta Sharing catalog | `SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES('<conn>')` |
| Metastore privileges | CREATE CATALOG/SHARE, USE PROVIDER/RECIPIENT | CREATE ZEROCOPY CONNECTOR, OPERATE/USAGE/MODIFY (connector), CREATE DATABASE, CREATE SHARE |

## Tool-by-tool mapping

| Tool | Original (Databricks) | Port (Snowflake) |
|------|-----------------------|------------------|
| `list_shares` | `workspace_client.shares.list()` | `SHOW SHARES` |
| `get_share_details` | `shares.get()` | `DESC SHARE` |
| `list_recipients` | `recipients.list()` | `SHOW ZEROCOPY CONNECTORS` + `SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES` |
| `create_or_update_share` | Delta share create + grant | `CREATE SHARE` + `GRANT … TO SHARE` |
| `create_or_update_share_csn` | CSN → Delta share | CSN → Snowflake share |
| `publish_data_product` | publish to BDC via SDK | `ALTER ZEROCOPY CONNECTOR … ADD SHARE` |
| `delete_share` | drop Delta share | `REMOVE SHARE` + `DROP SHARE` |
| `provision_share` | end-to-end DBX | end-to-end Snowflake (create→grant→publish) |
| `validate_share_readiness` | share + recipient grant | share objects + connector `CONNECTED` |
| `validate_databricks_privileges` → **`validate_snowflake_privileges`** | metastore privileges | connector/account privileges via `SHOW GRANTS` |
| `validate_tenant_hostname` | pure logic | **unchanged** (ported verbatim) |
| `check_deletion_vectors` → **`check_cld_asset_support`** | Delta deletion vectors (SAP Note 3706399) | unsupported object kinds for CLD/zero-copy |
| `list_unsupported_share_assets` | Delta materialized views | `SHOW OBJECTS` + unsupported-kind scan |
| `cleanup_orphaned_data_product` | orphan Delta share (SAP Note 3720724) | orphan Snowflake share recovery |
| `diagnose_share_error` | SAP-Note rule map | **same rules** + Snowflake connector-state hints |
| `generate_csn_template` | from Delta share | from Snowflake share (`DESC SHARE`) |
| `validate_ord_metadata` | pure logic | **unchanged** (ported verbatim) |

## Renamed tools

Two tools were renamed to reflect the platform (behavior preserved 1:1):

- `validate_databricks_privileges` → `validate_snowflake_privileges`
- `check_deletion_vectors` → `check_cld_asset_support`

## Preserved verbatim (platform-neutral)

`validate_tenant_hostname`, `validate_ord_metadata`, and the `diagnose_share_error`
SAP-Note rule table are SAP-side domain logic and are ported unchanged (the
diagnostics tool additionally gains Snowflake connector-state guidance).
