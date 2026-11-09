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

AI_VARS = ("AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AI_TIMEOUT")


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
