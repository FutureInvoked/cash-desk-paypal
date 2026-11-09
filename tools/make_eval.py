"""Build evals/match_eval.json from the fixture oracle (fixtures/cases.json).

Usage: python tools/make_eval.py      Score it: make eval  (uses the AI provider set in .env, if any)
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build() -> dict:
    cases = json.loads((ROOT / "fixtures" / "cases.json").read_text())
    items = []
    for name, c in cases.items():
        for key, outcome in c["expected"].items():
            items.append({"id": key, "kind": "invoice" if key.startswith("NW-") else "transaction",
                          "case": name, "expected_outcome": outcome,
                          "needs_ai": c.get("resolved_by") == "ai"})
    return {"description": "Expected reconciliation outcome per fixture row (Northwind Studio, as of 2026-11-07). "
                           "Items with needs_ai=true are unresolvable by the deterministic rules alone.",
            "as_of": "2026-11-07", "items": items}


if __name__ == "__main__":
    (ROOT / "evals").mkdir(exist_ok=True)
    (ROOT / "evals" / "match_eval.json").write_text(json.dumps(build(), indent=2) + "\n")
    print("wrote evals/match_eval.json")
