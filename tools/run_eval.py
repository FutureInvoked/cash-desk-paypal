"""Score the reconciler on evals/match_eval.json. Live Claude only if ANTHROPIC_API_KEY is set."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cashdesk.ai import run_eval  # noqa: E402
from cashdesk.paypal import FixturePayPalClient  # noqa: E402

if __name__ == "__main__":
    ev = json.loads((ROOT / "evals" / "match_eval.json").read_text())
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    score = run_eval(ev, c.list_invoices(), txns, as_of=date.fromisoformat(ev["as_of"]))
    print(json.dumps({k: v for k, v in score.items()}, indent=2))
    sys.exit(0 if score["accuracy"] == 1.0 else 1)
