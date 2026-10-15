#!/usr/bin/env python3
"""Review one section's diff with each configured Ollama model (supplements the Claude reviewer).

Usage (run in the background; needs --project so uv installs openai from this plugin):
    uv run --project {plugin_root} {plugin_root}/scripts/tools/ollama_code_review.py \
        --section-plan PLAN.md --diff section-NN-diff.md --output-dir {code_review_dir} --section-num NN

Writes section-NN-review-ollama-<slug>.md per model (as each finishes) and
section-NN-ollama-summary.json, and prints the summary. Always exits 0: Ollama trouble must
never block the Claude review or the commit.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.lib.ollama_client import OllamaSettings, chat, make_client, model_slug, resolve_settings

MAX_ERROR_CHARS = 500


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print to stderr and exit 2
        raise ValueError(message)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = _Parser(description="Ollama code review for one section")
    p.add_argument("--section-plan", required=True)
    p.add_argument("--diff", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--section-num", required=True)
    p.add_argument("--plugin-root", default=str(Path(__file__).resolve().parents[2]))
    return p.parse_args(argv)


def fill_user_prompt(template: str, plan_text: str, diff_text: str) -> str:
    """Single re.sub pass over {SECTION_PLAN}|{DIFF}; replacement text is never re-scanned."""
    values = {"SECTION_PLAN": plan_text, "DIFF": diff_text}
    return re.sub(r"\{(SECTION_PLAN|DIFF)\}", lambda m: values[m[1]], template)


def _clean(text: str, settings: OllamaSettings) -> str:
    """Never show credentials; cap length (a proxy's HTML error page shouldn't fill the files)."""
    text = text.replace(settings.base_url, settings.display_url)
    userinfo = urlsplit(settings.base_url).netloc.rpartition("@")[0]
    if userinfo:
        text = text.replace(userinfo, "***")
    return text[:MAX_ERROR_CHARS]


def _ollama_error(e: Exception, model: str, settings: OllamaSettings) -> str:
    import openai

    if isinstance(e, openai.NotFoundError):
        return f"model '{model}' not found on server — run `ollama pull {model}`"
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "rejected by server/proxy — check OLLAMA_API_KEY"
    if isinstance(e, openai.APITimeoutError):  # subclass of APIConnectionError: check first
        return f"timed out after {settings.timeout_seconds:g} s — raise ollama.timeout_seconds"
    if isinstance(e, openai.APIConnectionError):
        return f"cannot reach {settings.display_url} — is Ollama running / OLLAMA_HOST correct?"
    return str(e)  # incl. OllamaTruncationError / ValueError, whose messages explain themselves


def _chat_retrying_busy(client, model: str, system_prompt: str, user_prompt: str,
                        temperature: float | None) -> str:
    """Retry only 503 "server busy" (queue full, rejected before any work). Anything else,
    including a 500 that can arrive 25 minutes in, fails at once: a resend multiplies runtime."""
    import openai

    for delay in (5, 15, None):
        try:
            return chat(client, model, system_prompt, user_prompt, temperature=temperature)
        except openai.APIStatusError as e:
            if delay is None or e.status_code != 503 or "busy" not in str(e).lower():
                raise
            time.sleep(delay)


def review_section(system_prompt: str, user_prompt: str, settings: OllamaSettings,
                   on_result=None) -> dict[str, dict]:
    """Run each settings.models entry sequentially. Returns {f"ollama-{model_slug(m)}": result}.
    One model's failure never stops the next; on_result(key, result) fires as each finishes."""
    results = {}

    def record(m, result):
        key = f"ollama-{model_slug(m)}"
        results[key] = result
        if on_result:
            on_result(key, result)

    try:
        client = make_client(settings)
    except Exception as e:
        for m in settings.models:
            record(m, {"success": False, "model": m, "error": _clean(str(e), settings)})
        return results

    # ponytail: one host, sequential; per-host parallelism if multiple hosts are ever supported
    for m in settings.models:
        try:
            analysis = _chat_retrying_busy(client, m, system_prompt, user_prompt, settings.temperature)
            record(m, {"success": True, "model": m, "analysis": analysis})
        except Exception as e:
            record(m, {"success": False, "model": m, "error": _clean(_ollama_error(e, m, settings), settings)})
    return results


def _review_file_text(nn: str, result: dict) -> str:
    generated = datetime.now().isoformat()
    if result["success"]:
        return (f"# Ollama Code Review: Section {nn}\n\n**Model:** {result['model']}\n"
                f"**Generated:** {generated}\n\n---\n\n{result['analysis']}\n")
    return (f"# Ollama Code Review: Section {nn} - FAILED\n\n**Model:** {result['model']}\n"
            f"**Error:** {result['error']}\n**Generated:** {generated}\n")


def _run(args: argparse.Namespace, nn: str, out_dir: Path, summary: dict) -> None:
    plan_text = Path(args.section_plan).read_text()
    diff_text = Path(args.diff).read_text()
    root = Path(args.plugin_root)
    try:
        config = json.loads((root / "config.json").read_text())
    except Exception:
        config = {}
    if not isinstance(config, dict):
        config = {}
    try:
        settings = resolve_settings(config)
    except ValueError as e:  # malformed OLLAMA_HOST or ollama.temperature
        summary["ollama_status"] = f"invalid_config: {e}"
        return
    if settings is None:
        summary["ollama_status"] = "no_host"
        return
    if not settings.models:
        summary["ollama_status"] = "no_models"
        return

    prompts = root / "prompts" / "ollama_code_reviewer"
    system_prompt = (prompts / "system.md").read_text()
    user_prompt = fill_user_prompt((prompts / "user.md").read_text(), plan_text, diff_text)
    try:
        import openai  # noqa: F401
    except ImportError:
        summary["ollama_status"] = (f"error: openai not installed in the plugin environment "
                                    f"(run via uv run --project {root})")
        return

    summary["models"] = settings.models
    files = []

    def on_result(key, result):
        path = out_dir / f"section-{nn}-review-{key}.md"
        try:
            path.write_text(_review_file_text(nn, result))
            files.append(str(path.resolve()))
        except OSError as e:  # e.g. disk full: record it, keep reviewing the other models
            result = {**result, "success": False, "error": f"could not write {path.name}: {e}"[:MAX_ERROR_CHARS]}
        summary["reviews"][key] = {"success": result["success"], "model": result["model"],
                                   "error": result.get("error")}

    review_section(system_prompt, user_prompt, settings, on_result)
    summary["ollama_status"] = "ok"
    summary["files_written"] = files


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main() -> int:
    summary = {"ollama_status": None, "models": [], "files_written": [], "reviews": {}}
    out_dir = nn = None
    try:
        args = _parse_args(sys.argv[1:])
        if not re.fullmatch(r"\d+", args.section_num):
            raise ValueError(f"--section-num must be digits, got {args.section_num!r}")
        nn = args.section_num.zfill(2)
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        _run(args, nn, out_dir, summary)
    except Exception as e:  # never block the workflow; report instead
        summary["ollama_status"] = f"error: {str(e)[:MAX_ERROR_CHARS]}"
        summary["files_written"] = []
    text = json.dumps(summary, indent=2)
    if nn and out_dir and out_dir.is_dir():
        _write_atomic(out_dir / f"section-{nn}-ollama-summary.json", text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
