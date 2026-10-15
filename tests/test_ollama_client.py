"""Tests for the shared Ollama client (scripts/lib/ollama_client.py here) (no network)."""

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from scripts.lib import ollama_client as oc
from scripts.lib.ollama_client import (
    OllamaTruncationError,
    chat,
    make_client,
    model_slug,
    normalize_base_url,
    resolve_settings,
)


# --- normalize_base_url ---

@pytest.mark.parametrize("raw,base", [
    ("localhost", "http://localhost:11434/v1"),
    ("box:11434", "http://box:11434/v1"),
    ("http://box", "http://box/v1"),
    ("0.0.0.0:11434", "http://127.0.0.1:11434/v1"),
    ("10.0.0.0:11434", "http://10.0.0.0:11434/v1"),
    ("[::1]:11434", "http://[::1]:11434/v1"),
    ("https://proxy/ollama/", "https://proxy/ollama/v1"),
    ("http://box:11434/v1", "http://box:11434/v1"),
    ("http://box:11434/v1/", "http://box:11434/v1"),
    ("  box:11434 \n", "http://box:11434/v1"),
])
def test_normalize_base_url(raw, base):
    assert normalize_base_url(raw) == (base, base)


def test_normalize_base_url_redacts_userinfo():
    assert normalize_base_url("http://u:p@h:11434") == (
        "http://u:p@h:11434/v1", "http://***@h:11434/v1")


# --- model_slug ---

@pytest.mark.parametrize("model,slug", [
    ("qwen3-coder-next:latest", "qwen3-coder-next-latest"),
    ("hf.co/x/Y:Q2_K", "hf.co-x-y-q2_k"),
    ("--a//b::c--", "a-b-c"),
])
def test_model_slug(model, slug):
    assert model_slug(model) == slug


# --- resolve_settings ---

def test_resolve_settings_no_host(monkeypatch):
    assert resolve_settings({}) is None
    monkeypatch.setenv("OLLAMA_HOST", "  ")
    assert resolve_settings({}) is None


def test_env_models_override_configs(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    monkeypatch.setenv("OLLAMA_MODELS", "envmodel")
    s = resolve_settings({"ollama": {"models": ["sess"]}}, {"ollama": {"models": ["glob"]}})
    assert s.models == ["envmodel:latest"]


def test_session_beats_global_and_global_fallback(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    glob = {"ollama": {"models": ["glob"], "timeout_seconds": 60}}
    s = resolve_settings({"ollama": {"models": ["sess"], "timeout_seconds": 30}}, glob)
    assert (s.models, s.timeout_seconds) == (["sess:latest"], 30)
    s = resolve_settings({"plugin_root": "/x"}, glob)
    assert (s.models, s.timeout_seconds) == (["glob:latest"], 60)


def test_no_models_anywhere(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    s = resolve_settings({}, {"ollama": {"models": []}})
    assert s.models == []
    assert s.timeout_seconds == 1800


def test_env_models_trimmed_tagged_deduped(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    monkeypatch.setenv("OLLAMA_MODELS", " a , ,b ,qwen3, qwen3:latest,x/y:latest, x:y:latest")
    assert resolve_settings().models == ["a:latest", "b:latest", "qwen3:latest", "x/y:latest"]


def test_empty_session_models_fall_through_to_global(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    s = resolve_settings({"ollama": {"models": []}}, {"ollama": {"models": ["glob"]}})
    assert s.models == ["glob:latest"]


def test_string_models_split(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert resolve_settings({"ollama": {"models": "qwen3, b"}}).models == ["qwen3:latest", "b:latest"]


@pytest.mark.parametrize("host", ["box:abc", "box:99999"])
def test_bad_port_names_ollama_host(monkeypatch, host):
    monkeypatch.setenv("OLLAMA_HOST", host)
    with pytest.raises(ValueError, match="OLLAMA_HOST"):
        resolve_settings()


def test_api_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert resolve_settings().api_key == "ollama"
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    assert resolve_settings().api_key == "secret"


def test_display_url_in_settings(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://u:p@proxy")
    s = resolve_settings()
    assert s.base_url == "https://u:p@proxy/v1"
    assert s.display_url == "https://***@proxy/v1"


def test_conftest_clears_ollama_env():
    import os
    assert not any(k in os.environ for k in ("OLLAMA_HOST", "OLLAMA_MODELS", "OLLAMA_API_KEY"))


# --- make_client ---

@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    monkeypatch.setenv("OLLAMA_MODELS", "m")
    return resolve_settings({"ollama": {"timeout_seconds": 1234}})


@pytest.fixture
def recorder(monkeypatch):
    import openai
    calls = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: calls.append(kw) or kw)
    return calls


def test_make_client_defaults(settings, recorder):
    make_client(settings)
    kw = recorder[0]
    assert kw["max_retries"] == 0
    assert kw["base_url"] == "http://box:11434/v1"
    assert kw["api_key"] == "ollama"
    assert kw["timeout"] == openai.Timeout(1234, connect=10)


def test_make_client_timeout_override(settings, recorder):
    make_client(settings, timeout=10)
    assert recorder[0]["timeout"].read == 10


def test_list_models():
    client = SimpleNamespace(models=SimpleNamespace(
        list=lambda: [SimpleNamespace(id="a:latest"), SimpleNamespace(id="b:7b")]))
    assert oc.list_models(client) == ["a:latest", "b:7b"]


# --- chat ---

SYS, USER = "s" * 400, "u" * 3600  # estimate = 1000 tokens


def fake_client(content="answer", reasoning=None, finish_reason="stop",
                prompt_tokens=1000, usage=True, raises=None, calls=None):
    def create(**kw):
        if calls is not None:
            calls.append(kw)
        if raises:
            raise raises
        msg = SimpleNamespace(content=content, reasoning=reasoning)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=prompt_tokens) if usage else None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def run(**kw):
    return chat(fake_client(**kw), "m", SYS, USER)


def test_chat_normal():
    assert run(content="  answer \n") == "answer"


def test_chat_ignores_reasoning():
    assert run(reasoning="thinking hard") == "answer"


@pytest.mark.parametrize("content", [
    "<think>x</think>answer", "x</think>answer", "<think>a</think>b</think> answer"])
def test_chat_strips_think(content):
    assert run(content=content) == "answer"


@pytest.mark.parametrize("content", ["<think>unfinished", "", None, "   "])
def test_chat_empty_raises(content):
    with pytest.raises(ValueError, match="finish_reason=length"):
        run(content=content, finish_reason="length")


def test_chat_length_warning():
    out = run(finish_reason="length")
    assert out.startswith("answer")
    assert out.endswith("> WARNING: output truncated (context/num_predict limit reached)")


def test_chat_truncation_guard():
    with pytest.raises(OllamaTruncationError) as e:
        run(prompt_tokens=400)
    msg = str(e.value)
    assert "OLLAMA_CONTEXT_LENGTH" in msg and "400" in msg and "1000" in msg


@pytest.mark.parametrize("tokens", [700, 1000, 1300])
def test_chat_guard_passes_near_estimate(tokens):
    assert run(prompt_tokens=tokens) == "answer"


def test_chat_guard_skipped_without_usage():
    assert run(usage=False) == "answer"
    assert run(prompt_tokens=None) == "answer"
    assert run(prompt_tokens=0) == "answer"


def test_chat_sdk_error_propagates():
    err = RuntimeError("boom")
    with pytest.raises(RuntimeError) as e:
        run(raises=err)
    assert e.value is err


def test_chat_guard_raises_below_0_7():
    with pytest.raises(OllamaTruncationError):
        run(prompt_tokens=650)


# --- temperature ---

def test_temperature_unset(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert resolve_settings({}, {"ollama": {"temperature": None}}).temperature is None
    assert resolve_settings().temperature is None


def test_temperature_zero_kept(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert resolve_settings({"ollama": {"temperature": 0}}).temperature == 0.0


def test_temperature_precedence(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    glob = {"ollama": {"temperature": 0.9}}
    assert resolve_settings({"ollama": {"temperature": 0.2}}, glob).temperature == 0.2
    assert resolve_settings({"ollama": {"temperature": None}}, glob).temperature == 0.9


def test_temperature_first_non_null_wins_then_validated(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert resolve_settings({"ollama": {"temperature": 0.3}}, {"ollama": {"temperature": "bad"}}).temperature == 0.3
    with pytest.raises(ValueError):
        resolve_settings({"ollama": {"temperature": "bad"}}, {"ollama": {"temperature": 0.3}})


@pytest.mark.parametrize("bad", [True, "0.2", math.nan, math.inf])
def test_temperature_invalid(monkeypatch, bad):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    with pytest.raises(ValueError, match="ollama.temperature"):
        resolve_settings({"ollama": {"temperature": bad}})


def test_chat_omits_temperature_by_default():
    calls = []
    chat(fake_client(calls=calls), "m", SYS, USER)
    assert "temperature" not in calls[0]


def test_chat_sends_temperature():
    calls = []
    chat(fake_client(calls=calls), "m", SYS, USER, temperature=0.2)
    assert calls[0]["temperature"] == 0.2


# byte-identical with deep-plan's scripts/llm_clients/ollama_client.py; change both together
CLIENT_SHA256 = "91d81695f56ebc87cf0058ea1a4b0a6c685f6dd834f7b52a1f12bc4d254a258a"


def test_client_sha256_pinned():
    assert hashlib.sha256(Path(oc.__file__).read_bytes()).hexdigest() == CLIENT_SHA256
