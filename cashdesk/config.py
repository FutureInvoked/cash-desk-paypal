"""Runtime configuration read from the environment (never logs secret values)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


SANDBOX_HOST = "api-m.sandbox.paypal.com"


def sandbox_base_url() -> str:
    """The only PayPal base URL this app will ever call. Anything else (live included) is refused."""
    from urllib.parse import urlparse

    url = os.environ.get("PAYPAL_BASE_URL", SANDBOX_BASE_URL).rstrip("/")
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != SANDBOX_HOST or u.port not in (None, 443) or u.path not in ("", "/"):
        raise ValueError(f"refusing to use PayPal base URL {url!r}: only {SANDBOX_BASE_URL} is allowed")
    return SANDBOX_BASE_URL


@dataclass(frozen=True)
class Settings:
    paypal_mode: str
    var_dir: Path
    fixtures_dir: Path


def load_dotenv(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines from repo/.env into os.environ without overriding real env vars.

    Returns the names it set (never the values). No dependency; comments and malformed
    lines are skipped. Tests set CASHDESK_NO_DOTENV=1 so a developer's keys never leak in.
    """
    path = Path(path or REPO_ROOT / ".env")
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key.replace("_", "").isalnum() or " " in key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _has_keys() -> bool:
    return bool(os.environ.get("PAYPAL_CLIENT_ID")) and bool(os.environ.get("PAYPAL_CLIENT_SECRET"))


def load_settings() -> Settings:
    if not os.environ.get("CASHDESK_NO_DOTENV"):
        load_dotenv()
    mode = os.environ.get("PAYPAL_MODE") or ("sandbox" if _has_keys() else "fixtures")
    if mode not in {"fixtures", "sandbox"}:
        raise ValueError(f"PAYPAL_MODE must be 'fixtures' or 'sandbox', got {mode!r}")
    return Settings(
        paypal_mode=mode,
        var_dir=Path(os.environ.get("CASHDESK_VAR_DIR", REPO_ROOT / "var")),
        fixtures_dir=Path(os.environ.get("CASHDESK_FIXTURES_DIR", REPO_ROOT / "fixtures")),
    )


ALLOWED_HOST = r"(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*"


def check_startup() -> tuple[list[str], list[str]]:
    """(errors, warnings) for the settings the board starts with. An error stops the server with a
    message naming the setting; a warning is logged. Run it by hand: python -m cashdesk.config"""
    import re

    errors: list[str] = []
    warnings: list[str] = []
    try:
        s = load_settings()
    except ValueError as e:
        return [str(e)], []
    try:
        sandbox_base_url()
    except ValueError as e:
        errors.append(f"PAYPAL_BASE_URL: {e}")
    if s.paypal_mode == "sandbox" and not _has_keys():
        errors.append("PAYPAL_MODE=sandbox needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (in repo/.env or the "
                      "environment); to try it with sample data and no keys, use PAYPAL_MODE=fixtures")
    try:
        s.var_dir.mkdir(parents=True, exist_ok=True)
        probe = s.var_dir / f".write-check-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        errors.append(f"CASHDESK_VAR_DIR: can't write to {s.var_dir} ({e.strerror or e})")
    timeout = os.environ.get("AI_TIMEOUT")
    if timeout:
        try:
            float(timeout)
        except ValueError:
            errors.append(f"AI_TIMEOUT must be a number of seconds, got {timeout!r}")
    try:
        from .ai import make_reconciler
        make_reconciler()
    except ValueError as e:
        if "AI_TIMEOUT" not in str(e):
            errors.append(str(e))
    for h in os.environ.get("CASHDESK_ALLOWED_HOSTS", "").split(","):
        h = h.strip()
        if h and not re.fullmatch(ALLOWED_HOST, h):
            errors.append(f"CASHDESK_ALLOWED_HOSTS: {h!r} is not a host name (write desk.example.com: "
                          f"no https://, port or path)")
    if not errors and s.paypal_mode == "sandbox":
        from .users import UserStore
        approvers = [u for u in UserStore(s.var_dir / "cashdesk.db").list() if not u.demo and u.can_write]
        if not approvers:
            warnings.append('no approver accounts yet, so nobody can sign in: run '
                            'python -m cashdesk.users add <username> --name "Your Name"')
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    errors, warnings = check_startup()
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if errors:
        return 1
    s = load_settings()
    print(f"OK: PAYPAL_MODE={s.paypal_mode}, data in {s.var_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
