"""Ollama client via its OpenAI-compatible /v1 API.

Self-contained (stdlib + lazily imported openai) so other plugins can copy it unchanged.
Callers pass config dicts; nothing here reads lib/ or review.py.

Env: OLLAMA_HOST (required), OLLAMA_MODELS (comma list, overrides config),
OLLAMA_API_KEY (Bearer token for a reverse proxy; default "ollama").
Credentials in OLLAMA_HOST (http://user:pass@host) become Basic auth and replace the Bearer
token, so use one or the other.
"""

import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_PORT = 11434
LENGTH_WARNING = "> WARNING: output truncated (context/num_predict limit reached)"


@dataclass
class OllamaSettings:
    base_url: str          # normalized, ends with /v1 (may contain userinfo)
    display_url: str       # same, userinfo redacted — the only form ever printed
    api_key: str
    models: list[str]
    timeout_seconds: float
    temperature: float | None = None  # None: send nothing (Ollama /v1 then uses 1.0)


class OllamaTruncationError(Exception):
    """Server reported far fewer prompt tokens than were sent (context window too small)."""


def normalize_base_url(raw_host: str) -> tuple[str, str]:
    """Return (base_url, display_url): scheme/port defaults, 0.0.0.0 -> 127.0.0.1, single /v1."""
    raw = raw_host.strip()
    had_scheme = "://" in raw
    parts = urlsplit(raw if had_scheme else f"http://{raw}")
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"invalid port in OLLAMA_HOST: {e}") from None
    userinfo, at, hostport = parts.netloc.rpartition("@")
    if parts.hostname == "0.0.0.0":
        hostport = hostport.replace("0.0.0.0", "127.0.0.1", 1)
    if not had_scheme and port is None:
        hostport += f":{DEFAULT_PORT}"
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    path += "/v1"
    base = urlunsplit((parts.scheme, f"{userinfo}{at}{hostport}", path, "", ""))
    display = urlunsplit((parts.scheme, f"***@{hostport}" if at else hostport, path, "", ""))
    return base, display


def model_slug(model: str) -> str:
    """'qwen3-coder-next:latest' -> 'qwen3-coder-next-latest' (safe for filenames)."""
    return re.sub(r"[^a-z0-9._-]+", "-", model.lower()).strip("-")


def _normalize_models(models: list[str] | str) -> list[str]:
    if isinstance(models, str):
        models = models.split(",")
    out, seen = [], set()
    for m in (m.strip() for m in models):
        if not m:
            continue
        if ":" not in m.rsplit("/", 1)[-1]:
            m += ":latest"
        slug = model_slug(m)
        if slug not in seen:
            seen.add(slug)
            out.append(m)
    return out


def _from_configs(configs: tuple[dict, ...], key: str):
    for cfg in configs:
        block = (cfg or {}).get("ollama")
        # Empty values count as unset: session configs snapshot the global default `models: []`.
        if isinstance(block, dict) and block.get(key):
            return block[key]
    return None


def _temperature_from_configs(configs: tuple[dict, ...]) -> float | None:
    # Not _from_configs: 0 is a valid temperature, so only None/missing counts as unset.
    for cfg in configs:
        block = (cfg or {}).get("ollama")
        if isinstance(block, dict) and block.get("temperature") is not None:
            t = block["temperature"]
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
                raise ValueError(f"invalid ollama.temperature: {t!r} (must be a finite number)")
            return float(t)
    return None


def resolve_settings(*configs: dict) -> OllamaSettings | None:
    """None if OLLAMA_HOST is unset/blank. Configs in priority order (session, then global).
    May return empty models; callers treat that as 'not available'."""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return None
    models = os.environ.get("OLLAMA_MODELS", "").strip() or _from_configs(configs, "models") or []
    timeout = _from_configs(configs, "timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    base_url, display_url = normalize_base_url(host)
    return OllamaSettings(
        base_url=base_url,
        display_url=display_url,
        api_key=os.environ.get("OLLAMA_API_KEY") or "ollama",
        models=_normalize_models(models),
        timeout_seconds=float(timeout),
        temperature=_temperature_from_configs(configs),
    )


def make_client(settings: OllamaSettings, timeout: float | None = None):
    """OpenAI SDK client for the Ollama server. No SDK retries: a resend can cost many minutes."""
    import openai  # openai.Timeout re-exports the SDK's own HTTP library; no direct httpx import

    return openai.OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        max_retries=0,
        timeout=openai.Timeout(timeout or settings.timeout_seconds, connect=10),
    )


def list_models(client) -> list[str]:
    """Model IDs on the server. SDK errors propagate."""
    return [m.id for m in client.models.list()]


def chat(client, model: str, system_prompt: str, user_prompt: str,
         temperature: float | None = None) -> str:
    """One non-streaming completion; returns the final answer only (reasoning dropped).
    temperature is sent only when not None."""
    extra = {} if temperature is None else {"temperature": temperature}
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        **extra,
    )

    # ponytail: chars/4 and 0.7 are rough heuristics; a real tokenizer is the upgrade path.
    # Tokens per chars/4: 1.02 in the live check below, ~0.9 for prose-heavy markdown, ~1.1+ for
    # code, so 0.7 catches overflow from ~1.4x the context window up. Known ceiling: smaller
    # overflow slips through, and a very prose-heavy prompt (>~5.7 chars/token) could trip it.
    # Live check (Ollama 0.23.2, llama3.2:3b, same 10057-char prompt twice): prompt_tokens
    # 2564 then 2564 — counts include KV-cache-reused tokens, so the simple rule holds.
    # A 42K-char prompt on the 4096 default context reported 4096 and tripped this guard.
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    estimate = (len(system_prompt) + len(user_prompt)) / 4
    if prompt_tokens and prompt_tokens < 0.7 * estimate:  # 0/None: usage not reported
        raise OllamaTruncationError(
            f"server reported {prompt_tokens} prompt tokens but ~{int(estimate)} were sent; "
            "the context window is too small and the prompt was truncated. Set "
            "OLLAMA_CONTEXT_LENGTH=65536 (or more) on the server, or num_ctx in a Modelfile, "
            "and restart Ollama."
        )

    choice = response.choices[0]
    content = choice.message.content or ""
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    elif "<think>" in content:
        content = ""
    answer = content.strip()
    if not answer:
        reason = choice.finish_reason
        hint = " — context or output limit reached" if reason == "length" else ""
        raise ValueError(f"empty response (finish_reason={reason}{hint})")
    if choice.finish_reason == "length":
        answer += f"\n\n{LENGTH_WARNING}"
    return answer
