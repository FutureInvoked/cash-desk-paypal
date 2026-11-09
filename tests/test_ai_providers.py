"""AI providers: Claude, or any OpenAI-compatible chat-completions API (OpenAI, Mistral, Groq,
OpenRouter, Ollama, llama-server, LM Studio...), chosen in .env. The deterministic fallback runs
when no provider is configured. HTTP is always mocked here: no test touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cashdesk.ai import (ClaudeReconciler, FallbackReconciler, OpenAICompatibleReconciler,
                         make_reconciler, reconcile)
from tests.test_ai import AS_OF, LUMEN, data, good_payload
from tools.make_fixtures import txn_id

AI_VARS = ("AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AI_TIMEOUT", "CODEX_BIN")


@pytest.fixture
def clean_env(monkeypatch):
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


# ---------- choosing the provider ----------

def test_no_provider_configured_means_the_fallback(clean_env):
    r = make_reconciler()
    assert isinstance(r, FallbackReconciler)
    invs, txns = data()
    assert reconcile(invs, txns, as_of=AS_OF, reconciler=r).ai_status == "fallback (no AI provider set)"


def test_anthropic_key_picks_claude_by_default(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(make_reconciler(), ClaudeReconciler)


def test_openai_key_and_model_pick_the_openai_compatible_provider(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_env.setenv("AI_MODEL", "some-model")
    r = make_reconciler()
    assert isinstance(r, OpenAICompatibleReconciler)
    assert r.base_url == "https://api.openai.com/v1" and r.model == "some-model"


def test_explicit_provider_wins_over_auto(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clean_env.setenv("AI_PROVIDER", "openai")
    clean_env.setenv("AI_MODEL", "m")
    clean_env.setenv("AI_API_KEY", "k")
    assert isinstance(make_reconciler(), OpenAICompatibleReconciler)
    clean_env.setenv("AI_PROVIDER", "none")
    assert isinstance(make_reconciler(), FallbackReconciler)


def test_local_server_needs_no_key(clean_env):
    clean_env.setenv("AI_PROVIDER", "openai")
    clean_env.setenv("AI_BASE_URL", "http://localhost:11434/v1/")
    clean_env.setenv("AI_MODEL", "qwen3:8b")
    r = make_reconciler()
    assert isinstance(r, OpenAICompatibleReconciler)
    assert r.base_url == "http://localhost:11434/v1" and r.api_key is None


def test_openai_provider_without_a_model_falls_back_and_says_why(clean_env):
    clean_env.setenv("AI_PROVIDER", "openai")
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    r = make_reconciler()
    assert isinstance(r, FallbackReconciler)
    invs, txns = data()
    assert "AI_MODEL" in reconcile(invs, txns, as_of=AS_OF, reconciler=r).ai_status


def test_unknown_provider_is_an_error(clean_env):
    clean_env.setenv("AI_PROVIDER", "magic")
    with pytest.raises(ValueError, match="AI_PROVIDER"):
        make_reconciler()


# ---------- the OpenAI-compatible call (mocked HTTP) ----------

def chat_reply(content, finish_reason="stop", refusal=None):
    msg = {"role": "assistant", "content": content}
    if refusal:
        msg["refusal"] = refusal
    return {"choices": [{"index": 0, "finish_reason": finish_reason, "message": msg}]}


def mocked(reply=None, status=200, exc=None):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if exc:
            raise exc
        return httpx.Response(status, json=reply)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def rec(client, **kw):
    kw.setdefault("model", "test-model")
    kw.setdefault("base_url", "https://llm.example.com/v1")
    kw.setdefault("api_key", "sk-secret-123")
    return OpenAICompatibleReconciler(http=client, **kw)


def test_request_shape():
    invs, txns = data()
    client, calls = mocked(chat_reply(json.dumps({"resolutions": [], "reasons": []})))
    reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    req = calls[0]
    assert req.method == "POST" and str(req.url) == "https://llm.example.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-secret-123"
    body = json.loads(req.content)
    assert body["model"] == "test-model"
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert set(rf["json_schema"]["schema"]["required"]) == {"resolutions", "reasons"}
    system, user = body["messages"]
    assert system["role"] == "system" and "untrusted" in system["content"].lower()
    assert user["role"] == "user" and LUMEN in user["content"] and "NW-1021" in user["content"]


def test_no_authorization_header_without_a_key():
    invs, txns = data()
    client, calls = mocked(chat_reply(json.dumps({"resolutions": [], "reasons": []})))
    reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client, api_key=None))
    assert "authorization" not in calls[0].headers


def test_resolution_and_reasons_are_applied_with_the_same_guard_rails():
    invs, txns = data()
    ids = [t.id for t in txns] + [i.number for i in invs]
    client, _ = mocked(chat_reply(json.dumps(good_payload(ids))))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    t = r.txn(LUMEN)
    assert t.outcome == "matched" and t.rule == "ai" and t.resolved_by == "ai"
    assert t.reason_source == "ai" and "Lumen Bakery" in t.reason
    assert r.ai_status == "ai (test-model)"
    assert "sk-secret" not in r.ai_status


def test_ai_cannot_link_outside_its_candidates():
    invs, txns = data()
    bad = good_payload([])
    bad["resolutions"][0]["invoice_number"] = "NW-1001"
    client, _ = mocked(chat_reply(json.dumps(bad)))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert r.txn(LUMEN).outcome == "unmatched"


def test_json_inside_a_code_fence_is_accepted():
    # some local models wrap JSON in ```json fences even with response_format
    invs, txns = data()
    client, _ = mocked(chat_reply("```json\n" + json.dumps(good_payload([])) + "\n```"))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert r.txn(LUMEN).rule == "ai"


@pytest.mark.parametrize("kind", ["http_500", "network", "truncated", "refusal", "bad_json", "no_choices"])
def test_failures_fall_back_to_rules(kind):
    invs, txns = data()
    if kind == "http_500":
        client, _ = mocked({"error": {"message": "boom"}}, status=500)
    elif kind == "network":
        client, _ = mocked(exc=httpx.ConnectError("down"))
    elif kind == "truncated":
        client, _ = mocked(chat_reply('{"resolutions": [', finish_reason="length"))
    elif kind == "refusal":
        client, _ = mocked(chat_reply(None, refusal="I can't help with that."))
    elif kind == "bad_json":
        client, _ = mocked(chat_reply("not json"))
    else:
        client, _ = mocked({"choices": []})
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert r.txn(LUMEN).outcome == "matched" and r.txn(LUMEN).resolved_by == "fallback"
    assert r.ai_status.startswith("fallback") and "sk-secret" not in r.ai_status
    assert r.txn(txn_id("NW-1001")).reason_source == "rules"


def test_ai_timeout_is_configurable_for_slow_local_models(clean_env):
    clean_env.setenv("AI_PROVIDER", "openai")
    clean_env.setenv("AI_BASE_URL", "http://localhost:11434/v1")
    clean_env.setenv("AI_MODEL", "m")
    assert make_reconciler().http.timeout.read == 120.0
    clean_env.setenv("AI_TIMEOUT", "900")
    assert make_reconciler().http.timeout.read == 900.0
    clean_env.setenv("AI_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="AI_TIMEOUT"):
        make_reconciler()


# ---------- Codex CLI (ChatGPT sign-in) ----------

from cashdesk.ai import CodexReconciler  # noqa: E402


class FakeRun:
    """Stands in for subprocess.run: records the call and writes Codex's -o output file."""

    def __init__(self, reply=None, returncode=0, exc=None, write=True):
        self.reply, self.returncode, self.exc, self.write, self.calls = reply, returncode, exc, write, []

    def __call__(self, args, *, input, timeout, capture_output, text, cwd):
        self.calls.append({"args": args, "input": input, "timeout": timeout, "cwd": cwd})
        if self.exc:
            raise self.exc
        if self.write and self.reply is not None:
            out = args[args.index("-o") + 1]
            open(out, "w").write(self.reply if isinstance(self.reply, str) else json.dumps(self.reply))
        import subprocess
        return subprocess.CompletedProcess(args, self.returncode, "", "ERROR: boom" if self.returncode else "")


def test_codex_provider_is_opt_in(clean_env, tmp_path):
    clean_env.setenv("AI_PROVIDER", "codex")
    clean_env.setenv("CODEX_BIN", str(tmp_path / "codex"))
    (tmp_path / "codex").write_text("#!/bin/sh\n")
    r = make_reconciler()
    assert isinstance(r, CodexReconciler) and r.model == "gpt-6-sol" and r.timeout == 300.0
    clean_env.setenv("AI_MODEL", "gpt-6-luna")
    clean_env.setenv("AI_TIMEOUT", "120")
    r = make_reconciler()
    assert r.model == "gpt-6-luna" and r.timeout == 120.0


def test_codex_without_a_binary_falls_back_and_says_why(clean_env):
    clean_env.setenv("AI_PROVIDER", "codex")
    clean_env.setenv("CODEX_BIN", "/nonexistent/codex")
    r = make_reconciler()
    assert isinstance(r, FallbackReconciler) and "codex" in r.status.lower()


def test_codex_call_is_sandboxed_and_schema_bound():
    invs, txns = data()
    run = FakeRun({"resolutions": [], "reasons": []})
    res = reconcile(invs, txns, as_of=AS_OF, reconciler=CodexReconciler(codex_bin="/bin/codex", runner=run))
    (call,) = run.calls
    a = call["args"]
    assert a[0] == "/bin/codex" and a[1] == "exec" and a[-1] == "-"
    assert a[a.index("-s") + 1] == "read-only" and "--ephemeral" in a and "--skip-git-repo-check" in a
    assert a[a.index("-m") + 1] == "gpt-6-sol" and "mcp_servers={}" in a
    assert a[a.index("-C") + 1] == call["cwd"]
    assert "untrusted" in call["input"].lower() and LUMEN in call["input"] and "NW-1021" in call["input"]
    assert res.ai_status == "ai (codex: gpt-6-sol)"


def test_codex_resolution_goes_through_the_same_guard_rails():
    invs, txns = data()
    ids = [t.id for t in txns] + [i.number for i in invs]
    res = reconcile(invs, txns, as_of=AS_OF, reconciler=CodexReconciler(codex_bin="/bin/codex",
                                                                         runner=FakeRun(good_payload(ids))))
    t = res.txn(LUMEN)
    assert t.outcome == "matched" and t.rule == "ai" and t.reason_source == "ai"
    bad = good_payload([])
    bad["resolutions"][0]["invoice_number"] = "NW-1001"
    res = reconcile(invs, txns, as_of=AS_OF, reconciler=CodexReconciler(codex_bin="/bin/codex", runner=FakeRun(bad)))
    assert res.txn(LUMEN).outcome == "unmatched"


@pytest.mark.parametrize("kind", ["exit_1", "timeout", "no_output", "bad_json", "missing_binary"])
def test_codex_failures_fall_back_to_rules(kind):
    import subprocess
    invs, txns = data()
    run = {"exit_1": FakeRun({"resolutions": [], "reasons": []}, returncode=1),
           "timeout": FakeRun(exc=subprocess.TimeoutExpired("codex", 300)),
           "no_output": FakeRun(None),
           "bad_json": FakeRun("not json"),
           "missing_binary": FakeRun(exc=FileNotFoundError("codex"))}[kind]
    res = reconcile(invs, txns, as_of=AS_OF, reconciler=CodexReconciler(codex_bin="/bin/codex", runner=run))
    assert res.ai_status.startswith("fallback") and res.txn(LUMEN).resolved_by == "fallback"


# ---------- review findings #3, #8, #9 ----------

def test_codex_runs_with_its_file_and_shell_tools_switched_off():
    invs, txns = data()
    run = FakeRun({"resolutions": [], "reasons": []})
    reconcile(invs, txns, as_of=AS_OF, reconciler=CodexReconciler(codex_bin="/bin/codex", runner=run))
    a = run.calls[0]["args"]
    off = {a[i + 1] for i, x in enumerate(a) if x == "--disable"}
    assert {"shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use", "in_app_browser",
            "view_image"} <= off


def test_ai_reason_carrying_data_that_is_not_in_the_evidence_is_rejected():
    invs, txns = data()
    secret = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"
    tid = txn_id("NW-1001")
    p = {"resolutions": [], "reasons": [
        {"id": tid, "reason": f"Matched; key {secret}", "cited_fields": ["transaction_info.transaction_id"]},
        {"id": txn_id("NW-1002"), "reason": f"Matched to NW-1002 by transaction_info.transaction_id {txn_id('NW-1002')}.",
         "cited_fields": ["transaction_info.transaction_id"]}]}
    client, _ = mocked(chat_reply(json.dumps(p)))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert r.txn(tid).reason_source == "rules" and secret not in r.txn(tid).reason
    assert r.txn(txn_id("NW-1002")).reason_source == "ai"  # ids that are in the data are fine


def test_ai_resolution_reason_carrying_foreign_data_is_dropped():
    invs, txns = data()
    bad = good_payload([])
    header = "-" * 5 + "BEGIN OPENSSH " + "PRIVATE KEY" + "-" * 5  # split so the repo's secret scan stays clean
    bad["resolutions"][0]["reason"] = header + " b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ"
    client, _ = mocked(chat_reply(json.dumps(bad)))
    t = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client)).txn(LUMEN)
    assert "BEGIN" not in t.reason and "b3BlbnNz" not in t.reason


def test_reasons_go_to_the_exact_row_when_ids_repeat_across_event_codes():
    import dataclasses
    invs, txns = data()
    tid = txn_id("NW-1001")
    pay = next(t for t in txns if t.id == tid)
    conv = dataclasses.replace(pay, event_code="T0200", amount=-pay.amount, fee=None, invoice_ref=None,
                               paypal_invoice_id=None)
    txns = txns + [conv]
    p = {"resolutions": [], "reasons": [
        {"id": tid, "reason": "Plain id: ambiguous between two rows.", "cited_fields": ["transaction_info.transaction_id"]},
        {"id": f"{tid}:T0006", "reason": "Row-specific reason: transaction_info.transaction_id is the payment.",
         "cited_fields": ["transaction_info.transaction_id"]}]}
    client, calls = mocked(chat_reply(json.dumps(p)))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert f"{tid}:T0006" in calls[0].content.decode() and f"{tid}:T0200" in calls[0].content.decode()
    assert r.txn(tid, "T0006").reason == "Row-specific reason: transaction_info.transaction_id is the payment."
    assert r.txn(tid, "T0200").reason_source == "rules"


@pytest.mark.parametrize("reply", [
    {"choices": [{"message": {"content": [{"type": "text", "text": json.dumps({"resolutions": [], "reasons": []})}]}}]},
    {"choices": ["not a dict"]},
    {"choices": [{"message": "not a dict"}]},
    {"choices": [{"message": {"content": 42}}]},
])
def test_unusual_response_shapes_never_crash(reply):
    invs, txns = data()
    client, _ = mocked(reply)
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client))
    assert r.ai_status.startswith(("ai (", "fallback"))
