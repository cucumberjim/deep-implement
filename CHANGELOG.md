# Changelog

All notable changes to deep-implement will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Marketplace now lists `lodestone` (sibling MCP research-corpus plugin).
- Marketplace now lists `deep-sota` (sibling research-skill plugin that drives lodestone).

## [0.3.0] - 2026-10-15

### Added
- **Ollama code reviewers** (optional). Any Ollama-hosted model reviews each section's diff alongside the Claude code-reviewer. Opt in with `OLLAMA_HOST` plus `OLLAMA_MODELS` (or `ollama.models` in the new plugin-root `config.json`). An optional `OLLAMA_API_KEY` is sent as a Bearer token for proxies. Models run one at a time in the background; failures never block the section.
- `scripts/tools/ollama_code_review.py` and `prompts/ollama_code_reviewer/`. Each model writes `section-NN-review-ollama-<slug>.md`, and a `section-NN-ollama-summary.json` is always written.
- `scripts/lib/ollama_client.py`, byte-identical with deep-plan's copy (sha256 pin test). It adds an optional `ollama.temperature` and a context-truncation guard (fails the review when the server reports under 0.7 × the estimated prompt tokens).
- Step 7 triage across several reviews: duplicate findings merged with attribution, findings outside the diff dropped, every finding verified, no unverified auto-fix for Ollama-only findings, and a Reviewers status line in the interview transcript.
- `openai` runtime dependency (run the script via `uv run --project {plugin_root}`).

## [0.2.1] - 2026-02-28

### Fixed
- **Plugin root discovery** — SessionStart hook now injects `DEEP_PLUGIN_ROOT` into Claude's context via `additionalContext`, eliminating slow `find` commands for script discovery. Falls back to filename-based search that works with both hyphen and underscore directory naming (fixes marketplace install path mismatch). ([piercelamb/deep-project#3](https://github.com/piercelamb/deep-project/issues/3))

## [0.2.0] - 2026-01-30

### Changed
- **Unified session ID** - Changed `DEEP_IMPLEMENT_SESSION_ID` to shared `DEEP_SESSION_ID`
- **Normalized env var** - Changed `CLAUDE_SESSION_ID` to `DEEP_SESSION_ID` in env file writes and all scripts
- SessionStart hook now checks if `DEEP_SESSION_ID` already matches before outputting
- Prevents duplicate output when multiple deep-* plugins run together

## [0.1.0] - 2025-01-01

### Added
- Initial release
- Section-by-section implementation workflow from /deep-plan output
- TDD methodology with code review at each step
- Git commits with review trails
- SessionStart hook for session ID capture
