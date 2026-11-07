"""R5: security pass. No secrets in the tree or history, ignore rules, exact pins, sandbox guard."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def test_gitignore_covers_secrets_and_state():
    gi = (ROOT / ".gitignore").read_text().split()
    for rule in (".env", ".env.*", "var/*", "*.db", "*.jsonl", ".venv/"):
        assert rule in gi, rule


def test_no_secret_or_state_file_was_ever_committed():
    names = set(git("log", "--all", "--pretty=format:", "--name-only").split())
    bad = [n for n in names if n == ".env" or (n.startswith(".env.") and n != ".env.example")
           or n.endswith((".db", ".sqlite", ".jsonl", ".head")) or n.startswith("var/") and n != "var/.gitkeep"]
    assert not bad, bad


def test_secret_scan_of_full_history_is_clean():
    out = subprocess.run(["bash", "tools/secret_scan.sh"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_direct_dependencies_are_pinned_exactly():
    pp = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = pp["dependencies"] + pp["optional-dependencies"]["dev"]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[0-9][\w.]*", d) for d in deps), deps


def test_lockfile_pins_every_transitive_dependency():
    lock = (ROOT / "requirements.lock").read_text()
    reqs = [l.split(";")[0].strip() for l in lock.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert len(reqs) >= 10
    assert all("==" in r for r in reqs), [r for r in reqs if "==" not in r]
    for name in ("fastapi", "uvicorn", "httpx", "anthropic", "pytest", "starlette", "pydantic"):
        assert any(r.lower().startswith(name + "==") for r in reqs), name


def test_makefile_installs_from_the_lock():
    mk = (ROOT / "Makefile").read_text()
    assert "requirements.lock" in mk and "--no-deps" in mk


def test_sandbox_guard_and_licence_present():
    assert (ROOT / "tests" / "test_sandbox_guard.py").exists()
    assert "MIT License" in (ROOT / "LICENSE").read_text()


def test_no_brand_images_in_repo():
    imgs = [p for p in ROOT.rglob("*") if p.suffix.lower() in {".png", ".svg", ".ico", ".gif"}
            and ".venv" not in p.parts]
    assert not imgs, imgs


def test_secret_scan_catches_a_planted_key(tmp_path):
    import shutil
    repo = tmp_path / "r"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "secret_scan.sh", repo / "tools" / "secret_scan.sh")
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    fake = "PAYPAL_CLIENT_SECRET=" + "EFAKE" + "x" * 20  # assembled so this file never matches itself
    (repo / "config.txt").write_text(fake + "\n")
    run("git", "add", "."); run("git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x")
    # remove it in a later commit: history must still be flagged
    (repo / "config.txt").write_text("clean\n")
    run("git", "add", "."); run("git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "y")
    out = run("bash", "tools/secret_scan.sh")
    assert out.returncode == 1 and "config.txt" in out.stdout
    assert "EFAKE" not in out.stdout  # never prints the value
