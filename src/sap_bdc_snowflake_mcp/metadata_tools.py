"""Metadata tools: ORD validation and CSN template generation (Snowflake port)."""

from __future__ import annotations

import json
import re

from .config import BDCConfig
from .snowflake_client import SnowflakeClient, SnowflakeError, quote_ident, quote_literal
from .csn_builder import validate_csn, diff_csn, render_csn_docs

# ---------------------------------------------------------------------------
# ORD validation constants and helpers (ported verbatim from extended_tools.py)
# ---------------------------------------------------------------------------

_VISIBILITY_VALUES = {"public", "interval", "private"}
_RELEASE_STATUS_VALUES = {"active", "beta", "deprecated"}


def _is_iso8601(s: str) -> bool:
    if not isinstance(s, str):
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$", s))


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_validate_ord_metadata(arguments: dict, client, cfg) -> str:
    """Validate ORD JSON. Pure-logic — no Snowflake call."""
    ord_obj = arguments.get("ord") or {}
    errors: list = []
    warnings: list = []

    if isinstance(ord_obj, dict) and "@openResourceDiscoveryV1" in ord_obj and isinstance(
        ord_obj["@openResourceDiscoveryV1"], dict
    ):
        ord_obj = ord_obj["@openResourceDiscoveryV1"]

    for field in ("title", "shortDescription", "description"):
        v = ord_obj.get(field) if isinstance(ord_obj, dict) else None
        if not v or not isinstance(v, str) or not v.strip():
            errors.append(f"Required field '{field}' is missing or empty")

    short = (ord_obj.get("shortDescription") if isinstance(ord_obj, dict) else "") or ""
    desc = (ord_obj.get("description") if isinstance(ord_obj, dict) else "") or ""
    if short and desc and short.strip() and short.strip() in desc:
        errors.append(
            "ORD rule: 'description' must NOT contain the 'shortDescription' value. "
            "Rewrite description to avoid quoting shortDescription verbatim."
        )

    vis = ord_obj.get("visibility") if isinstance(ord_obj, dict) else None
    if vis is not None and vis not in _VISIBILITY_VALUES:
        errors.append(f"visibility must be one of {sorted(_VISIBILITY_VALUES)}; got {vis!r}")

    rs = ord_obj.get("releaseStatus") if isinstance(ord_obj, dict) else None
    if rs is not None and rs not in _RELEASE_STATUS_VALUES:
        errors.append(f"releaseStatus must be one of {sorted(_RELEASE_STATUS_VALUES)}; got {rs!r}")

    dep = ord_obj.get("deprecationDate") if isinstance(ord_obj, dict) else None
    sun = ord_obj.get("sunsetDate") if isinstance(ord_obj, dict) else None
    if dep is not None and not _is_iso8601(dep):
        errors.append(f"deprecationDate must be ISO 8601; got {dep!r}")
    if sun is not None and not _is_iso8601(sun):
        errors.append(f"sunsetDate must be ISO 8601; got {sun!r}")
    if dep and sun and _is_iso8601(dep) and _is_iso8601(sun) and sun < dep:
        errors.append(
            f"sunsetDate ({sun}) must be greater than or equal to deprecationDate ({dep})"
        )

    if isinstance(ord_obj, dict):
        if not ord_obj.get("industry"):
            warnings.append("Optional 'industry' is empty — recommended for data product discovery")
        if not ord_obj.get("lineOfBusiness"):
            warnings.append("Optional 'lineOfBusiness' is empty — recommended for data product discovery")

    return json.dumps({
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "spec_reference": "https://sap.github.io/csn-interop-specification/",
    }, indent=2)


def handle_generate_csn_template(arguments: dict, client: SnowflakeClient, cfg: BDCConfig) -> str:
    """Generate a CSN template from an existing Snowflake share.

    Runs DESC SHARE to enumerate TABLE/VIEW objects, then builds a skeleton
    CSN definition with empty elements. Column-level elements must be filled
    in by the user — only object names are available from the share descriptor.
    """
    share_name = (arguments.get("share_name") or "").strip()
    if not share_name:
        return "❌ 'share_name' argument is required"

    try:
        rows = client.execute(f"DESC SHARE {quote_ident(share_name)}")
    except SnowflakeError as exc:
        return f"❌ Failed to describe share '{share_name}': {exc}"

    definitions: dict = {}
    for row in rows:
        kind = (row.get("kind") or "").strip().upper()
        if kind not in ("TABLE", "VIEW"):
            continue
        fqn = (row.get("name") or "").strip()
        # Use the last segment of the fully-qualified name as the entity key
        entity_name = fqn.split(".")[-1] if fqn else fqn
        if entity_name:
            definitions[entity_name] = {"kind": "entity", "elements": {}}

    csn_template = {
        "csn": "https://sap.com/csn/2.0",
        "definitions": definitions,
    }

    note = (
        "// NOTE: 'elements' for each entity is empty. "
        "Fill in column definitions (name, type, etc.) based on your table schemas."
    )
    return (
        f"CSN template for share '{share_name}' "
        f"({len(definitions)} table/view object(s) found).\n"
        f"{note}\n\n"
        f"{json.dumps(csn_template, indent=2)}"
    )


def handle_validate_csn(arguments: dict, client, cfg) -> str:
    """Structurally validate a CSN document (pure-logic)."""
    csn = arguments.get("csn")
    if csn is None:
        return "❌ 'csn' argument is required."
    report = validate_csn(csn)
    head = ("✅ CSN is valid." if report["valid"]
            else f"❌ CSN has {len(report['errors'])} error(s).")
    if report.get("warnings"):
        head += f" ({len(report['warnings'])} warning(s))"
    return head + "\n\n" + json.dumps(report, indent=2)


def handle_diff_csn(arguments: dict, client, cfg) -> str:
    """Diff two CSN documents into breaking vs non-breaking changes (pure-logic)."""
    old = arguments.get("old_csn")
    new = arguments.get("new_csn")
    if old is None or new is None:
        return "❌ both 'old_csn' and 'new_csn' arguments are required."
    report = diff_csn(old, new)
    compatible = report["summary"].get("compatible")
    head = ("✅ Compatible — no breaking changes." if compatible
            else f"❌ {report['summary']['breaking']} breaking change(s) detected.")
    return head + "\n\n" + json.dumps(report, indent=2)


def handle_render_csn_docs(arguments: dict, client, cfg) -> str:
    """Render a CSN document as Markdown documentation (pure-logic)."""
    csn = arguments.get("csn")
    if csn is None:
        return "❌ 'csn' argument is required."
    return render_csn_docs(csn)



# ---------------------------------------------------------------------------
# MCP tool registry
# ---------------------------------------------------------------------------

SCHEMAS: list[dict] = [
    {
        "name": "validate_ord_metadata",
        "description": (
            "Validate an ORD (Open Resource Discovery) metadata object against SAP BDC rules. "
            "Checks required fields (title, shortDescription, description), enum values for "
            "visibility and releaseStatus, ISO 8601 date formats, and sunset/deprecation ordering. "
            "Returns a JSON report with valid, errors, and warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ord": {
                    "type": "object",
                    "description": "ORD metadata object to validate (top-level or wrapped in @openResourceDiscoveryV1)",
                },
            },
            "required": ["ord"],
        },
    },
    {
        "name": "generate_csn_template",
        "description": (
            "Generate a CSN (Core Schema Notation) template from an existing Snowflake share. "
            "Reads the share's TABLE and VIEW objects via DESC SHARE and produces a skeleton "
            "CSN definition. Column-level elements must be filled in by the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_name": {
                    "type": "string",
                    "description": "Name of the Snowflake share",
                },
            },
            "required": ["share_name"],
        },
    },
    {
        "name": "validate_csn",
        "description": (
            "Structurally validate a CSN (Core Schema Notation) document before publishing a "
            "SAP BDC data product. Checks for a non-empty 'definitions' object, per-entity "
            "'elements', element 'type' presence, recognized cds.* types, and key elements. "
            "Returns a JSON report with valid, errors, and warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "csn": {"type": "object", "description": "The CSN document to validate."},
            },
            "required": ["csn"],
        },
    },
    {
        "name": "diff_csn",
        "description": (
            "Diff two CSN documents and classify changes as breaking (entity/element removed, "
            "kind/type/key changed) or non-breaking (entity/element added, type widened). Use "
            "before re-publishing a versioned data product to catch compatibility regressions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_csn": {"type": "object", "description": "The previously published CSN."},
                "new_csn": {"type": "object", "description": "The new CSN to compare."},
            },
            "required": ["old_csn", "new_csn"],
        },
    },
    {
        "name": "render_csn_docs",
        "description": (
            "Render a CSN document as human-readable Markdown (one table per entity, with "
            "element type, key marker, and label)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "csn": {"type": "object", "description": "The CSN document to document."},
            },
            "required": ["csn"],
        },
    },
]

HANDLERS: dict = {
    "validate_ord_metadata": handle_validate_ord_metadata,
    "generate_csn_template": handle_generate_csn_template,
    "validate_csn": handle_validate_csn,
    "diff_csn": handle_diff_csn,
    "render_csn_docs": handle_render_csn_docs,
}
