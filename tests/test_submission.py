"""R2: the Devpost text has every section Devpost asks for and only claims what the code backs."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "submission" / "devpost.md").read_text()


def test_devpost_sections():
    for h in ("## Inspiration", "## What it does", "## How we built it", "## Challenges we ran into",
              "## Accomplishments", "## What we learned", "## What's next", "## Built with"):
        assert h in TEXT, h


def test_states_who_it_is_for_and_the_three_prize_angles():
    low = TEXT.lower()
    assert "small business" in low
    assert "ag grid community" in low and "approval" in low and "claude" in low


def test_numbers_match_the_code():
    # fixtures: 26 invoices, 26 transactions; eval 31 items; refund cap 1,000; rate cap 10/hour
    assert "26 invoices" in TEXT and "26 PayPal transactions" in TEXT
    assert "31" in TEXT and "1,000" in TEXT and "10 per hour" in TEXT
    assert "claude-sonnet-5-5" in TEXT and "36.2.0" in TEXT


def test_sandbox_status_is_honest_until_s3():
    # Until the sandbox run (S3) is recorded in docs/, the text must not claim live sandbox results.
    evidence = list((ROOT / "docs").glob("sandbox-evidence*.md"))
    if not evidence:
        assert re.search(r"sandbox", TEXT, re.I)
        assert "UPDATE AFTER S3" in TEXT


def test_length_is_reasonable():
    words = len(TEXT.split())
    assert 500 <= words <= 1600, words


SCRIPT = (ROOT / "submission" / "video-script.md").read_text() if (ROOT / "submission" / "video-script.md").exists() else ""


def _beats():
    # rows like: | 0:00-0:12 | ... |
    out = []
    for a, b in re.findall(r"^\|\s*(\d+:\d\d)\s*[-–]\s*(\d+:\d\d)\s*\|", SCRIPT, re.M):
        s = lambda t: int(t.split(":")[0]) * 60 + int(t.split(":")[1])
        out.append((s(a), s(b)))
    return out


def test_video_runs_under_three_minutes_with_contiguous_beats():
    beats = _beats()
    assert len(beats) >= 7
    assert beats[0][0] == 0
    for (a1, b1), (a2, b2) in zip(beats, beats[1:]):
        assert b1 == a2 and a1 < b1
    assert beats[-1][1] <= 170  # 2:50, leaves margin under the 3:00 limit


def test_voiceover_fits_the_time():
    # ~150 words a minute spoken at a calm pace
    vo = " ".join(re.findall(r"^> (.+)$", SCRIPT, re.M))
    assert vo and len(vo.split()) <= 150 * _beats()[-1][1] / 60


def test_shots_show_the_judged_things():
    low = SCRIPT.lower()
    for s in ("reconciliation", "evidence", "propose actions", "approve", "api-m.sandbox.paypal.com",
              "paypal-request-id", "audit", "make demo", "ag grid community", "claude"):
        assert s in low, s
    assert "logo" not in low
