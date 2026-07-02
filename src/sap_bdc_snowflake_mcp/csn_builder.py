"""Generate SAP CSN (Core Schema Notation) documents and publish-back SQL.

Supports the SAP BDC Connect "publish" workflow (share a Snowflake object back to
SAP BDC as a data product):
  * convert a Snowflake table/view (column metadata) into a CSN entity
  * convert a Snowflake semantic view (semantic model) into a multi-entity CSN,
    mapping DIMENSION -> element, FACT/METRIC -> measure element (@Aggregation)
  * build the CREATE SHARE + GRANT + SHARE_BACK + ADD SHARE SQL sequence

CSN type mapping follows CSN Interop (cds.* primitive types).
"""

from __future__ import annotations

import json
import re


def snowflake_type_to_cds(type_str: str | None) -> dict:
    """Map a Snowflake data type (e.g. 'NUMBER(38,0)', 'VARCHAR(50)') to a CSN element."""
    t = (type_str or "").upper().strip()
    base = re.split(r"[(\s]", t, 1)[0]
    m = re.search(r"\(([^)]*)\)", t)
    args = [a.strip() for a in m.group(1).split(",")] if m else []

    def _int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    if base in ("VARCHAR", "STRING", "TEXT", "CHAR", "CHARACTER", "NVARCHAR", "NCHAR", "NVARCHAR2"):
        el = {"type": "cds.String"}
        if args and _int(args[0]):
            el["length"] = _int(args[0])
        return el
    if base in ("NUMBER", "NUMERIC", "DECIMAL"):
        prec = _int(args[0]) if args else None
        scale = _int(args[1]) if len(args) > 1 else 0
        if (scale or 0) == 0:
            return {"type": "cds.Integer"} if (prec or 0) <= 9 else {"type": "cds.Integer64"}
        el = {"type": "cds.Decimal"}
        if prec:
            el["precision"] = prec
        if scale is not None:
            el["scale"] = scale
        return el
    if base in ("INT", "INTEGER", "SMALLINT", "TINYINT", "BYTEINT"):
        return {"type": "cds.Integer"}
    if base == "BIGINT":
        return {"type": "cds.Integer64"}
    if base in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL"):
        return {"type": "cds.Double"}
    if base == "BOOLEAN":
        return {"type": "cds.Boolean"}
    if base == "DATE":
        return {"type": "cds.Date"}
    if base == "TIME":
        return {"type": "cds.Time"}
    if base in ("DATETIME", "TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"):
        return {"type": "cds.Timestamp"}
    if base in ("BINARY", "VARBINARY"):
        return {"type": "cds.Binary"}
    if base in ("VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY"):
        return {"type": "cds.LargeString"}
    return {"type": "cds.String"}


def _nn(nullable) -> bool:
    return str(nullable).strip().lower() in ("n", "not_null", "false", "no")


def build_csn_from_columns(namespace: str, entity: str, columns: list[dict],
                           label: str | None = None, keys: list[str] | None = None) -> dict:
    """Build a single-entity CSN from column metadata rows (name/type/comment/nullable)."""
    key_set = {k.upper() for k in (keys or [])}
    elements: dict = {}
    for col in columns:
        name = col.get("name")
        if not name:
            continue
        el = snowflake_type_to_cds(col.get("type"))
        if col.get("comment"):
            el["@EndUserText.label"] = col["comment"]
        if name.upper() in key_set:
            el["key"] = True
        if _nn(col.get("nullable")):
            el["notNull"] = True
        elements[name] = el
    ent = {"kind": "entity", "elements": elements}
    if label:
        ent["@EndUserText.label"] = label
    return {"csn": "https://sap.com/csn/2.0", "$version": "2.0",
            "definitions": {f"{namespace}.{entity}": ent}}


def build_csn_from_semantic_view(rows: list[dict], namespace: str) -> tuple[dict, list[str]]:
    """Convert a DESC SEMANTIC VIEW result into a multi-entity CSN.

    Returns (csn, base_tables) where base_tables are the underlying physical
    tables (DB.SCHEMA.TABLE) that must be granted to the share.
    """
    tables: dict = {}    # logical name -> {pk, comment, base:{db,schema,name}}
    members: dict = {}   # (kind,name) -> {kind, table, type, comment}
    for r in rows:
        kind = r.get("object_kind")
        name = r.get("object_name")
        prop = r.get("property")
        val = r.get("property_value")
        if kind == "TABLE":
            t = tables.setdefault(name, {"pk": [], "comment": None, "base": {}})
            if prop == "PRIMARY_KEY":
                try:
                    t["pk"] = json.loads(val)
                except (TypeError, ValueError, json.JSONDecodeError):
                    t["pk"] = []
            elif prop == "COMMENT":
                t["comment"] = val
            elif prop == "BASE_TABLE_DATABASE_NAME":
                t["base"]["db"] = val
            elif prop == "BASE_TABLE_SCHEMA_NAME":
                t["base"]["schema"] = val
            elif prop == "BASE_TABLE_NAME":
                t["base"]["name"] = val
        elif kind in ("DIMENSION", "FACT", "METRIC"):
            m = members.setdefault((kind, name), {"kind": kind, "table": None, "type": None, "comment": None})
            if prop == "TABLE":
                m["table"] = val
            elif prop == "DATA_TYPE":
                m["type"] = val
            elif prop == "COMMENT":
                m["comment"] = val

    definitions: dict = {}
    base_tables: list[str] = []
    for tname, tinfo in tables.items():
        pk = {x.upper() for x in tinfo["pk"]}
        elements: dict = {}
        for (kind, mname), m in members.items():
            if m["table"] != tname:
                continue
            el = snowflake_type_to_cds(m["type"])
            if m["comment"]:
                el["@EndUserText.label"] = m["comment"]
            if mname.upper() in pk:
                el["key"] = True
            if kind in ("FACT", "METRIC"):
                el["@Aggregation.default"] = "#SUM"
                el["@Semantics.Measure"] = True
            else:
                el["@Semantics.Dimension"] = True
            elements[mname] = el
        ent = {"kind": "entity", "elements": elements}
        if tinfo["comment"]:
            ent["@EndUserText.label"] = str(tinfo["comment"]).strip()
        definitions[f"{namespace}.{tname}"] = ent
        b = tinfo["base"]
        if b.get("db") and b.get("schema") and b.get("name"):
            base_tables.append(f'{b["db"]}.{b["schema"]}.{b["name"]}')

    csn = {"csn": "https://sap.com/csn/2.0", "$version": "2.0", "definitions": definitions}
    return csn, sorted(set(base_tables))


def build_ord_metadata(title: str, description: str) -> dict:
    """Minimal ORD (Open Resource Discovery) metadata object for the data product."""
    short = (description or title)[:250]
    return {
        "title": title,
        "shortDescription": short,
        "description": description or title,
        "visibility": "public",
        "releaseStatus": "active",
        "version": "1.0.0",
    }


def build_publish_sql(share: str, tables: list[str], connector_fqn: str,
                      description: str = "") -> str:
    """Build the CREATE SHARE + GRANT + SHARE_BACK + ADD SHARE SQL sequence."""
    dbs, schemas = set(), set()
    for t in tables:
        parts = t.split(".")
        if len(parts) == 3:
            dbs.add(parts[0])
            schemas.add(f"{parts[0]}.{parts[1]}")
    comment = description.replace("'", "''")[:250] if description else ""
    lines = ["-- 1) Create the share",
             f"CREATE SHARE IF NOT EXISTS {share}"
             + (f"\n  COMMENT = '{comment}'" if comment else "") + ";",
             "",
             "-- 2) Grant the object(s) to the share"]
    for d in sorted(dbs):
        lines.append(f"GRANT USAGE ON DATABASE {d} TO SHARE {share};")
    for s in sorted(schemas):
        lines.append(f"GRANT USAGE ON SCHEMA {s} TO SHARE {share};")
    for t in tables:
        lines.append(f"GRANT SELECT ON TABLE {t} TO SHARE {share};")
    lines += ["",
              "-- 3) Enable share-back and associate the share with the connector",
              f"ALTER ZEROCOPY CONNECTOR {connector_fqn} SET SHARE_BACK = TRUE;",
              f"ALTER ZEROCOPY CONNECTOR {connector_fqn} ADD SHARE {share};",
              "",
              "-- 4) Publish as a SAP BDC data product with the ORD metadata + CSN below.",
              "--    Run via the MCP 'publish_data_product' tool, or SYSTEM$SAP_PUBLISH_DATA_PRODUCT",
              "--    passing the ORD JSON and CSN JSON payloads.",
              "-- NOTE: publish-back requires Iceberg V3 tables (CATALOG='SNOWFLAKE',",
              "--       STORAGE_SERIALIZATION_POLICY='COMPATIBLE', ENABLE_ICEBERG_MERGE_ON_READ=FALSE)."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CSN quality: validation, diff, and documentation.
#
# These capabilities were inspired by the contract-checking design in
# Rahul Sethi's SAP BDC MCP project (https://github.com/rahulsethi/SAPBDCMCP,
# PolyForm Noncommercial License). No source was copied — the logic below is an
# independent implementation for the SAP BDC Connect publish workflow.
# ---------------------------------------------------------------------------

_KNOWN_CDS_TYPES = {
    "cds.String", "cds.LargeString", "cds.Integer", "cds.Integer64", "cds.Decimal",
    "cds.Double", "cds.Boolean", "cds.Date", "cds.Time", "cds.DateTime",
    "cds.Timestamp", "cds.Binary", "cds.LargeBinary", "cds.UUID",
}

# CDS type widenings that are NOT breaking (old -> new).
_SAFE_WIDENINGS = {
    ("cds.String", "cds.LargeString"),
    ("cds.Integer", "cds.Integer64"),
    ("cds.Integer", "cds.Decimal"),
    ("cds.Integer64", "cds.Decimal"),
    ("cds.Decimal", "cds.Double"),
}


def validate_csn(csn: dict) -> dict:
    """Structurally validate a CSN document. Returns {valid, errors, warnings}."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(csn, dict):
        return {"valid": False, "errors": ["CSN root must be a JSON object."], "warnings": []}

    defs = csn.get("definitions")
    if not isinstance(defs, dict) or not defs:
        errors.append("CSN must contain a non-empty 'definitions' object.")
        defs = {}

    for ename, edef in defs.items():
        where = f"definitions['{ename}']"
        if not isinstance(edef, dict):
            errors.append(f"{where} must be an object.")
            continue
        kind = edef.get("kind")
        if not kind:
            warnings.append(f"{where} has no 'kind' (assuming 'entity').")
        elif kind != "entity":
            warnings.append(f"{where} kind is '{kind}' (expected 'entity').")
        elements = edef.get("elements")
        if not isinstance(elements, dict) or not elements:
            errors.append(f"{where} must have a non-empty 'elements' object.")
            continue
        has_key = False
        for cname, cdef in elements.items():
            cwhere = f"{where}.elements['{cname}']"
            if not isinstance(cdef, dict):
                errors.append(f"{cwhere} must be an object.")
                continue
            ctype = cdef.get("type")
            if not ctype:
                errors.append(f"{cwhere} is missing 'type'.")
            elif not isinstance(ctype, str):
                errors.append(f"{cwhere} 'type' must be a string.")
            elif ctype not in _KNOWN_CDS_TYPES:
                warnings.append(f"{cwhere} type '{ctype}' is not a recognized cds.* type.")
            if cdef.get("key"):
                has_key = True
        if not has_key:
            warnings.append(f"{where} has no key element (no primary key).")

    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "entity_count": len(defs)}


def diff_csn(old_csn: dict, new_csn: dict) -> dict:
    """Diff two CSN documents into breaking vs non-breaking changes."""
    old_defs = (old_csn or {}).get("definitions") or {}
    new_defs = (new_csn or {}).get("definitions") or {}
    if not isinstance(old_defs, dict) or not isinstance(new_defs, dict):
        return {"breaking": [{"code": "INVALID_INPUT", "message": "Invalid CSN structure."}],
                "non_breaking": [], "summary": {"error": "invalid input"}}

    breaking: list[dict] = []
    non_breaking: list[dict] = []

    old_names, new_names = set(old_defs), set(new_defs)
    for name in sorted(old_names - new_names):
        breaking.append({"code": "ENTITY_REMOVED", "entity": name})
    for name in sorted(new_names - old_names):
        non_breaking.append({"code": "ENTITY_ADDED", "entity": name})

    for name in sorted(old_names & new_names):
        oe, ne = old_defs[name], new_defs[name]
        if not isinstance(oe, dict) or not isinstance(ne, dict):
            continue
        if oe.get("kind") and ne.get("kind") and oe["kind"] != ne["kind"]:
            breaking.append({"code": "KIND_CHANGED", "entity": name,
                             "old": oe["kind"], "new": ne["kind"]})
        oel = oe.get("elements") or {}
        nel = ne.get("elements") or {}
        for col in sorted(set(oel) - set(nel)):
            breaking.append({"code": "ELEMENT_REMOVED", "entity": name, "element": col})
        for col in sorted(set(nel) - set(oel)):
            item = {"code": "ELEMENT_ADDED", "entity": name, "element": col}
            (breaking if nel[col].get("key") else non_breaking).append(
                {**item, "note": "new key element"} if nel[col].get("key") else item)
        for col in sorted(set(oel) & set(nel)):
            ot = (oel[col] or {}).get("type")
            nt = (nel[col] or {}).get("type")
            if ot and nt and ot != nt:
                if (ot, nt) in _SAFE_WIDENINGS:
                    non_breaking.append({"code": "TYPE_WIDENED", "entity": name,
                                         "element": col, "old": ot, "new": nt})
                else:
                    breaking.append({"code": "TYPE_CHANGED", "entity": name,
                                     "element": col, "old": ot, "new": nt})
            if bool((oel[col] or {}).get("key")) != bool((nel[col] or {}).get("key")):
                breaking.append({"code": "KEY_CHANGED", "entity": name, "element": col})

    return {"breaking": breaking, "non_breaking": non_breaking,
            "summary": {"breaking": len(breaking), "non_breaking": len(non_breaking),
                        "compatible": not breaking}}


def render_csn_docs(csn: dict) -> str:
    """Render a CSN document as a human-readable Markdown reference."""
    defs = (csn or {}).get("definitions") or {}
    if not isinstance(defs, dict) or not defs:
        return "_No CSN definitions to document._"
    lines: list[str] = []
    for ename in sorted(defs):
        edef = defs[ename] if isinstance(defs[ename], dict) else {}
        label = edef.get("@EndUserText.label")
        lines.append(f"### {ename}")
        if label:
            lines.append(f"*{label}*")
        lines.append("")
        lines.append("| Element | Type | Key | Label |")
        lines.append("|---|---|:--:|---|")
        for cname, cdef in (edef.get("elements") or {}).items():
            cdef = cdef if isinstance(cdef, dict) else {}
            typ = cdef.get("type", "")
            if "length" in cdef:
                typ += f"({cdef['length']})"
            elif "precision" in cdef:
                typ += f"({cdef['precision']},{cdef.get('scale', 0)})"
            key = "🔑" if cdef.get("key") else ""
            lines.append(f"| {cname} | {typ} | {key} | {cdef.get('@EndUserText.label', '')} |")
        lines.append("")
    return "\n".join(lines)
