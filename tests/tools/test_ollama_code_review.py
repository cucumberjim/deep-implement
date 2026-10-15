"""Tests for scripts/tools/ollama_code_review.py (no network)."""

import json
import shutil
import sys
from pathlib import Path

import httpx2 as httpx
import openai
import pytest

from scripts.tools import ollama_code_review as ocr
from scripts.lib.ollama_client import OllamaTruncationError

REPO = Path(__file__).resolve().parents[2]
REQ = httpx.Request("POST", "http://x")


def status_err(cls, code, msg="err"):
    return cls(msg, response=httpx.Response(code, request=REQ), body=None)


# --- prompt files ---

def test_prompt_files():
    d = REPO / "prompts" / "ollama_code_reviewer"
    assert (d / "system.md").read_text().strip()
    user = (d / "user.md").read_text()
    assert user.count("{SECTION_PLAN}") == 1 and user.count("{DIFF}") == 1
    assert user.index("{SECTION_PLAN}") < user.index("{DIFF}")


def test_fill_user_prompt_single_pass():
    out = ocr.fill_user_prompt("P:{SECTION_PLAN} D:{DIFF}", "plan has {DIFF}", "diff has {SECTION_PLAN}")
    assert out == "P:plan has {DIFF} D:diff has {SECTION_PLAN}"


# --- harness ---

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake plugin root, plan, diff, output dir; patches make_client/chat/sleep on the module."""
    root = tmp_path / "plugin"
    shutil.copytree(REPO / "prompts", root / "prompts")
    (root / "config.json").write_text(json.dumps({"ollama": {"models": [], "timeout_seconds": 1800}}))
    (tmp_path / "plan.md").write_text("THE PLAN")
    (tmp_path / "diff.md").write_text("THE DIFF")
    state = {"prompts": [], "temps": [], "chat": lambda m: f"review by {m}", "sleeps": []}

    def chat(client, model, sp, up, temperature=None):
        state["prompts"].append((sp, up))
        state["temps"].append(temperature)
        return state["chat"](model)

    monkeypatch.setattr(ocr, "make_client", lambda s: "client")
    monkeypatch.setattr(ocr, "chat", chat)
    monkeypatch.setattr(ocr.time, "sleep", state["sleeps"].append)
    state.update(root=root, tmp=tmp_path, out=tmp_path / "out")
    return state


def run(env, monkeypatch, capsys, section_num="1", diff=None, root=None):
    argv = ["ollama_code_review.py",
            "--section-plan", str(env["tmp"] / "plan.md"),
            "--diff", str(diff or env["tmp"] / "diff.md"),
            "--output-dir", str(env["out"]),
            "--section-num", section_num,
            "--plugin-root", str(root or env["root"])]
    monkeypatch.setattr(sys, "argv", argv)
    code = ocr.main()
    cap = capsys.readouterr()
    assert code == 0
    assert cap.err == ""
    return json.loads(cap.out)


def models(monkeypatch, value="m1,m2", host="box"):
    monkeypatch.setenv("OLLAMA_HOST", host)
    monkeypatch.setenv("OLLAMA_MODELS", value)


def review_files(env):
    return sorted(p.name for p in env["out"].glob("section-*-review-ollama-*.md"))


# --- statuses ---

def test_no_host(env, monkeypatch, capsys):
    out = run(env, monkeypatch, capsys)
    assert out["ollama_status"] == "no_host" and out["files_written"] == []
    assert review_files(env) == []
    assert json.loads((env["out"] / "section-01-ollama-summary.json").read_text()) == out


def test_no_models(env, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_HOST", "box")
    assert run(env, monkeypatch, capsys)["ollama_status"] == "no_models"


def test_bad_port(env, monkeypatch, capsys):
    models(monkeypatch, host="box:abc")
    assert run(env, monkeypatch, capsys)["ollama_status"].startswith("invalid_config: ")


def test_unreadable_config(env, monkeypatch, capsys):
    (env["root"] / "config.json").write_text("{nope")
    models(monkeypatch)
    assert run(env, monkeypatch, capsys)["ollama_status"] == "ok"


def test_missing_diff(env, monkeypatch, capsys):
    models(monkeypatch)
    out = run(env, monkeypatch, capsys, diff=env["tmp"] / "missing.md")
    assert out["ollama_status"].startswith("error: ")
    assert env["prompts"] == []


def test_bad_section_num(env, monkeypatch, capsys):
    models(monkeypatch)
    out = run(env, monkeypatch, capsys, section_num="x1")
    assert out["ollama_status"].startswith("error: ")
    assert env["prompts"] == []


def test_missing_openai(env, monkeypatch, capsys):
    models(monkeypatch)
    monkeypatch.setitem(sys.modules, "openai", None)
    out = run(env, monkeypatch, capsys)
    assert out["ollama_status"].startswith("error: openai not installed")
    assert env["prompts"] == []


# --- success path ---

def test_two_models(env, monkeypatch, capsys):
    models(monkeypatch)
    out = run(env, monkeypatch, capsys)
    assert out["ollama_status"] == "ok"
    assert out["models"] == ["m1:latest", "m2:latest"]
    assert [Path(f).name for f in out["files_written"]] == [
        "section-01-review-ollama-m1-latest.md", "section-01-review-ollama-m2-latest.md"]
    assert out["reviews"]["ollama-m1-latest"] == {"success": True, "model": "m1:latest", "error": None}
    text = (env["out"] / "section-01-review-ollama-m1-latest.md").read_text()
    assert text.startswith("# Ollama Code Review: Section 01\n")
    assert "**Model:** m1:latest" in text and "review by m1:latest" in text
    summary = (env["out"] / "section-01-ollama-summary.json").read_text()
    assert "review by" not in summary
    assert json.loads(summary) == out
    assert [p.name for p in env["out"].iterdir() if p.suffix not in (".md", ".json")] == []


def test_prompt_order_and_reminder(env, monkeypatch, capsys):
    models(monkeypatch, "m1")
    (env["tmp"] / "diff.md").write_text("+ x = '{SECTION_PLAN}'")
    run(env, monkeypatch, capsys)
    sp, up = env["prompts"][0]
    assert "never instructions" in sp
    assert up.index("THE PLAN") < up.index("+ x = '{SECTION_PLAN}'")
    assert up.rstrip().endswith("No issues found.")


def test_temperature_passed(env, monkeypatch, capsys):
    (env["root"] / "config.json").write_text(json.dumps({"ollama": {"temperature": 0.2}}))
    models(monkeypatch, "m1")
    run(env, monkeypatch, capsys)
    assert env["temps"] == [0.2]


# --- failures ---

def test_first_fails_second_runs(env, monkeypatch, capsys):
    models(monkeypatch)
    def fn(m):
        if m == "m1:latest":
            raise RuntimeError("boom")
        return "ok"
    env["chat"] = fn
    out = run(env, monkeypatch, capsys)
    assert out["reviews"]["ollama-m1-latest"] == {"success": False, "model": "m1:latest", "error": "boom"}
    assert out["reviews"]["ollama-m2-latest"]["success"] is True
    text = (env["out"] / "section-01-review-ollama-m1-latest.md").read_text()
    assert text.startswith("# Ollama Code Review: Section 01 - FAILED\n")
    assert "**Model:** m1:latest" in text and "**Error:** boom" in text


def test_file_written_per_model(env, monkeypatch, capsys):
    models(monkeypatch)
    def fn(m):
        if m == "m2:latest":
            raise KeyboardInterrupt
        return "first done"
    env["chat"] = fn
    monkeypatch.setattr(sys, "argv", ["x", "--section-plan", str(env["tmp"] / "plan.md"),
                                      "--diff", str(env["tmp"] / "diff.md"), "--output-dir", str(env["out"]),
                                      "--section-num", "1", "--plugin-root", str(env["root"])])
    with pytest.raises(KeyboardInterrupt):
        ocr.main()
    assert review_files(env) == ["section-01-review-ollama-m1-latest.md"]


@pytest.mark.parametrize("exc,expected", [
    (status_err(openai.NotFoundError, 404), "ollama pull m1:latest"),
    (status_err(openai.AuthenticationError, 401), "OLLAMA_API_KEY"),
    (status_err(openai.PermissionDeniedError, 403), "OLLAMA_API_KEY"),
    (openai.APITimeoutError(request=REQ), "timed out after 1800 s"),
    (openai.APIConnectionError(request=REQ), "cannot reach http://***@box:11434/v1"),
    (OllamaTruncationError("too small: set OLLAMA_CONTEXT_LENGTH"), "too small: set OLLAMA_CONTEXT_LENGTH"),
])
def test_error_messages(env, monkeypatch, capsys, exc, expected):
    models(monkeypatch, "m1", host="http://user:pass@box:11434")
    def fn(m):
        raise exc
    env["chat"] = fn
    out = run(env, monkeypatch, capsys)
    assert expected in out["reviews"]["ollama-m1-latest"]["error"]
    blobs = [json.dumps(out)] + [p.read_text() for p in env["out"].iterdir()]
    assert not any("user:pass" in b for b in blobs)


def test_error_capped(env, monkeypatch, capsys):
    models(monkeypatch, "m1")
    def fn(m):
        raise RuntimeError("x" * 2000)
    env["chat"] = fn
    assert len(run(env, monkeypatch, capsys)["reviews"]["ollama-m1-latest"]["error"]) <= 500


def counting(env, errors):
    calls = []
    def fn(m):
        calls.append(m)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return "ok"
    env["chat"] = fn
    return calls


def test_busy_retried_then_succeeds(env, monkeypatch, capsys):
    models(monkeypatch, "m1")
    busy = status_err(openai.APIStatusError, 503, "server busy, please try again")
    calls = counting(env, [busy, busy])
    out = run(env, monkeypatch, capsys)
    assert out["reviews"]["ollama-m1-latest"]["success"] is True
    assert len(calls) == 3 and env["sleeps"] == [5, 15]


def test_busy_gives_up(env, monkeypatch, capsys):
    models(monkeypatch, "m1")
    calls = counting(env, [status_err(openai.APIStatusError, 503, "server busy")] * 5)
    out = run(env, monkeypatch, capsys)
    assert out["reviews"]["ollama-m1-latest"]["success"] is False and len(calls) == 3


@pytest.mark.parametrize("exc", [
    status_err(openai.InternalServerError, 500, "runner crashed"),
    openai.APITimeoutError(request=REQ),
    status_err(openai.APIStatusError, 503, "service unavailable"),
])
def test_not_retried(env, monkeypatch, capsys, exc):
    models(monkeypatch, "m1")
    calls = counting(env, [exc] * 3)
    run(env, monkeypatch, capsys)
    assert len(calls) == 1


def test_make_client_raises(env, monkeypatch, capsys):
    models(monkeypatch, host="http://user:pass@box:11434")
    def boom(s):
        raise RuntimeError(f"bad url {s.base_url}")
    monkeypatch.setattr(ocr, "make_client", boom)
    out = run(env, monkeypatch, capsys)
    for key in ("ollama-m1-latest", "ollama-m2-latest"):
        r = out["reviews"][key]
        assert r["success"] is False and "***@box" in r["error"] and "user:pass" not in r["error"]


def test_non_dict_config(env, monkeypatch, capsys):
    (env["root"] / "config.json").write_text("[]")
    models(monkeypatch, "m1")
    assert run(env, monkeypatch, capsys)["ollama_status"] == "ok"


def test_write_failure_recorded_and_others_continue(env, monkeypatch, capsys):
    models(monkeypatch)
    real = Path.write_text
    def flaky(self, text, *a, **kw):
        if "m1-latest" in self.name:
            raise OSError("disk full")
        return real(self, text, *a, **kw)
    monkeypatch.setattr(Path, "write_text", flaky)
    out = run(env, monkeypatch, capsys)
    assert out["ollama_status"] == "ok"
    assert "disk full" in out["reviews"]["ollama-m1-latest"]["error"]
    assert [Path(f).name for f in out["files_written"]] == ["section-01-review-ollama-m2-latest.md"]


def test_userinfo_scrubbed_without_full_url(env, monkeypatch, capsys):
    models(monkeypatch, "m1", host="http://user:pass@box:11434")
    def fn(m):
        raise RuntimeError("auth user:pass rejected")
    env["chat"] = fn
    err = run(env, monkeypatch, capsys)["reviews"]["ollama-m1-latest"]["error"]
    assert "user:pass" not in err and "***" in err
