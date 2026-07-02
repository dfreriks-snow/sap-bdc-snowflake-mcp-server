# SAP BDC Snowflake MCP Server

An [MCP](https://modelcontextprotocol.io) server that manages **SAP Business Data
Cloud (BDC)** integration on **Snowflake** — via the **SAP BDC Connect zero-copy
connector** — and runs inside **Cortex Code**.

It exposes **20 tools** for discovering, provisioning, validating, publishing, and
troubleshooting SAP BDC data products on Snowflake.

> **Port note.** This is a Snowflake port of the Databricks-based
> [`sap-bdc-mcp-server`](https://github.com/MarioDeFelipe/sap-bdc-mcp-server) by
> Mario DeFelipe (MIT). The Databricks Delta-Sharing model (recipients, shares,
> Unity Catalog, `databricks-sdk`) is remapped to the Snowflake **zero-copy
> connector** model (`ZEROCOPY CONNECTOR … PARTNER = SAP_BDC`, Snowflake shares,
> catalog-linked databases). See [`docs/CONVERSION.md`](docs/CONVERSION.md).

---

## How it works

SAP BDC Connect for Snowflake uses a **zero-copy connector** to bridge SAP BDC and
Snowflake with no data movement:

- **Inbound** — SAP publishes data products that appear to Snowflake through the
  connector (`SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES`) and are consumed as
  **catalog-linked databases**.
- **Outbound (share-back)** — a Snowflake **share** is associated with the
  connector (`ALTER ZEROCOPY CONNECTOR … SET SHARE_BACK=TRUE; ADD SHARE`) to
  publish a Snowflake dataset back to the SAP BDC catalog as a data product.

This server drives that lifecycle over the Snowflake SQL API using
`snowflake-connector-python`.

---

## Prerequisites

- Python 3.9+
- A Snowflake account with **SAP BDC Connect enabled** and a **connected**
  zero-copy connector (`SHOW ZEROCOPY CONNECTORS IN ACCOUNT` shows `CONNECTED`).
  Setup of the connector itself is covered by the `sap-bdc-connect-for-snowflake`
  Cortex Code skill.
- A role with the connector privileges (see `validate_snowflake_privileges`):
  `CREATE ZEROCOPY CONNECTOR`, `OPERATE`/`USAGE`/`MODIFY` on the connector,
  `CREATE DATABASE`, `CREATE SHARE`.
- A named connection in `~/.snowflake/connections.toml` (key-pair, SSO, or password).

## Install

```bash
git clone <this-repo> && cd sap-bdc-snowflake-mcp-server
python3 -m pip install -e .
cp .env.example .env      # then edit
```

## Configure

Set your Snowflake connection + connector in `.env` (or as env vars):

```env
SNOWFLAKE_CONNECTION=dfreriksdemo          # named connection in connections.toml
BDC_CONNECTOR_NAME=SAP_BDC_CONNECT_ZC
BDC_CONNECTOR_DATABASE=SAP_BDC_CONNECT
BDC_CONNECTOR_SCHEMA=PUBLIC
```

## Register in Cortex Code

```bash
cortex mcp add sap-bdc-snowflake \
  "$(command -v sap-bdc-snowflake-mcp)" \
  --transport stdio \
  -e SNOWFLAKE_CONNECTION=dfreriksdemo \
  -e BDC_CONNECTOR_NAME=SAP_BDC_CONNECT_ZC
```

(Or point at the module directly: `python -m sap_bdc_snowflake_mcp.server`.)
After adding, the `mcp__sap-bdc-snowflake__*` tools become available in Cortex Code.

---

## Tools (17)

| Tool | What it does (Snowflake) |
|------|--------------------------|
| `list_shares` | `SHOW SHARES` — share-back data products |
| `get_share_details` | `DESC SHARE` — objects granted to a share |
| `list_recipients` | Zero-copy connector(s) + inbound SAP data products (`SYSTEM$ZEROCOPY_CONNECTOR_LIST_SHARES`) |
| `create_or_update_share` | `CREATE SHARE` + grant tables |
| `create_or_update_share_csn` | Create share from a CSN schema |
| `publish_data_product` | Associate share with connector (`SET SHARE_BACK=TRUE; ADD SHARE`) |
| `delete_share` | Remove from connector + `DROP SHARE` |
| `provision_share` | End-to-end: create → grant → publish |
| `validate_share_readiness` | Share exists + has objects + connector CONNECTED |
| `validate_snowflake_privileges` | Checks the connector privileges on the current role |
| `validate_tenant_hostname` | SAP tenant hostname rules (SAP Notes 3652165 / 3705747) |
| `check_cld_asset_support` | Lists existing catalog-linked databases (call with no args), or scans one and flags object types unsupported for CLD consumption |
| `list_unsupported_share_assets` | Flags assets that can't be shared to SAP BDC |
| `generate_csn_template` | CSN template from a share's objects |
| `validate_csn` | Structurally validate a CSN document (definitions, elements, cds.* types, keys) |
| `diff_csn` | Classify CSN changes as breaking vs non-breaking between two versions |
| `render_csn_docs` | Render a CSN document as Markdown |
| `validate_ord_metadata` | Validate ORD JSON before publishing |
| `diagnose_share_error` | Map an error to the relevant SAP Note + connector-state fix |
| `cleanup_orphaned_data_product` | Orphaned data-product recovery (SAP Note 3720724) |

## Development

```bash
python3 -m pip install -e ".[dev]"
pytest
```

## Acknowledgments

- Databricks original: [`sap-bdc-mcp-server`](https://github.com/MarioDeFelipe/sap-bdc-mcp-server) by Mario DeFelipe (MIT).
- CSN quality tools (`validate_csn`, `diff_csn`, `render_csn_docs`) were **inspired by** the contract-checking design in [Rahul Sethi's SAP BDC MCP](https://github.com/rahulsethi/SAPBDCMCP) (PolyForm Noncommercial). No source was copied — see [`NOTICE`](NOTICE).

## License

MIT — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Original Databricks work
© Mario DeFelipe; Snowflake port © 2026 contributors.
