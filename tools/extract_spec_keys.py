"""Extract the allowed property names per schema from PayPal's official OpenAPI specs.

Usage: python tools/extract_spec_keys.py <dir with spec json files>
Specs: https://github.com/paypal/paypal-rest-api-specifications/tree/main/openapi
Writes tests/spec_keys.json, which the fixture tests use to prove that every key in
fixtures/ exists in the real response schema (field for field).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOTS = {
    "reporting_transactions_v1.json": ["search_response"],
    "invoicing_v2.json": ["invoices"],
}


def _ref(r: str) -> str:
    return r.split("/")[-1]


def _child(v: dict) -> str | None:
    """Schema name a property points to (following arrays and single allOf refs)."""
    if "$ref" in v:
        return _ref(v["$ref"])
    if v.get("type") == "array" and "items" in v:
        return _child(v["items"])
    for part in v.get("allOf", []):
        c = _child(part)
        if c:
            return c
    return None


def _props(schemas: dict, name: str, seen=None) -> dict:
    seen = seen or set()
    if name in seen:
        return {}
    seen.add(name)
    sc = schemas[name]
    out: dict = {}
    for part in [sc] + sc.get("allOf", []) + sc.get("oneOf", []):
        if "$ref" in part and part is not sc:
            out.update(_props(schemas, _ref(part["$ref"]), seen))
            continue
        for sub in part.get("allOf", []) if part is not sc else []:
            if "$ref" in sub:
                out.update(_props(schemas, _ref(sub["$ref"]), seen))
        for k, v in part.get("properties", {}).items():
            out[k] = _child(v)
    # a list schema (type array) points at its item schema
    if sc.get("type") == "array" and "items" in sc:
        out["__items__"] = _child(sc["items"])
    return out


def extract(spec_dir: Path) -> dict:
    result: dict = {}
    for fname, roots in ROOTS.items():
        schemas = json.loads((spec_dir / fname).read_text())["components"]["schemas"]
        todo = list(roots)
        while todo:
            n = todo.pop()
            key = f"{fname.split('.')[0]}:{n}"
            if key in result:
                continue
            props = _props(schemas, n)
            result[key] = props
            todo += [c for c in props.values() if c and f"{fname.split('.')[0]}:{c}" not in result]
    return result


if __name__ == "__main__":
    out = extract(Path(sys.argv[1]))
    dest = Path(__file__).resolve().parent.parent / "tests" / "spec_keys.json"
    dest.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {dest} ({len(out)} schemas)")
