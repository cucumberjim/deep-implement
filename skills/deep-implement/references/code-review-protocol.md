# Code Review Protocol

Code review workflow for /deep-implement: a dedicated Claude subagent, plus optional Ollama-hosted reviewers that run alongside it.

## Overview

This protocol covers running the code-reviewer subagent.

After implementing a section, before committing:

1. Track files created during implementation
2. Stage all changes (new and modified)
3. Generate diff and write to `{state_dir}/code_review/section-NN-diff.md`
4. Start the Ollama reviewers in the background
5. Launch code-reviewer subagent
6. Write review to file
7. Wait for the Ollama reviewers, read their summary

Then proceed to the Code Review Interview (Step 7 in SKILL.md).

## Directory Structure

Create `code_review/` inside the state directory:

```
planning/
├── sections/
│   ├── index.md
│   └── section-NN-*.md
└── implementation/               # state_dir
    ├── deep_implement_config.json
    └── code_review/              # Created by this workflow
        ├── section-01-diff.md       # Diff input for subagent
        ├── section-01-review.md     # Review output from subagent
        ├── section-01-review-ollama-<model-slug>.md  # One per Ollama model (if configured)
        ├── section-01-ollama-summary.json             # Ollama run summary (always written)
        ├── section-01-interview.md  # Interview transcript (written by interview step)
        ├── section-02-diff.md
        ├── section-02-review.md
        ├── section-02-interview.md
        └── ...
```

## Step Details

### 1. Track Created Files

During implementation, maintain a list of files created.

This is needed because `git add -u` only stages **modified tracked files**, not new files.

### 2. Stage Changes

```bash
# Stage NEW files explicitly
git add path/to/new/file1.py path/to/new/file2.py ...

# Stage MODIFIED tracked files
git add -u
```

### 3. Generate Diff and Write to File

```bash
git diff --staged > {code_review_dir}/section-NN-diff.md
```

If the diff is empty, skip review and proceed to commit. (The Ollama reviewers are not started either.)

### 4. Start Ollama Reviewers (Background)

Ollama-hosted models (any model on a local or remote Ollama server) review the same diff and section plan as extra reviewers. They never replace the Claude subagent.

First delete any old summary so a stale one from an earlier diff is never read:

```bash
rm -f {code_review_dir}/section-NN-ollama-summary.json
```

Then start the script with the Bash tool's **`run_in_background: true`**:

```bash
uv run --project {plugin_root} {plugin_root}/scripts/tools/ollama_code_review.py \
  --section-plan {sections_dir}/section-NN-<name>.md \
  --diff {code_review_dir}/section-NN-diff.md \
  --output-dir {code_review_dir} --section-num NN
```

- **`--project {plugin_root}` is required.** Without it uv resolves the project from the current directory (the user's target repo) and `openai` isn't installed. Use absolute paths.
- **Always start it.** When Ollama isn't configured (`OLLAMA_HOST` unset or no models) it finishes immediately and writes a summary saying so.
- **Why background:** models run one at a time with up to `ollama.timeout_seconds` (default 1800 s) each, so a run can take models × 30 min. A foreground Bash call stops at 10 minutes.

The script writes `section-NN-review-ollama-<slug>.md` per model (failed models too) and always writes `section-NN-ollama-summary.json`, then exits 0:

```json
{"ollama_status": "ok", "models": ["qwen3-coder-next:latest"],
 "files_written": ["/abs/.../section-01-review-ollama-qwen3-coder-next-latest.md"],
 "reviews": {"ollama-qwen3-coder-next-latest": {"success": true, "model": "...", "error": null}}}
```

`ollama_status` is `ok`, `no_host`, `no_models`, `invalid_config: <msg>` or `error: <msg>`. `files_written` is empty unless `ok`. Review text lives only in the files.

### 5. Launch Code Reviewer Subagent

Launch the `code-reviewer` subagent with both the section plan and the diff:

```
Task:
  subagent_type: "code-reviewer"
  description: "Review section NN code"
  prompt: |
    Review this implementation:
    - Section plan: {sections_dir}/section-NN-<name>.md
    - Code changes: {code_review_dir}/section-NN-diff.md
```

The subagent:
- Has tools: `Read, Grep, Glob` (NO Write)
- Reads the section plan to understand requirements
- Reads the diff to see what was implemented
- Compares implementation against the plan
- Returns JSON with section name and freeform review

**Subagent returns:**
```json
{
  "section": "section-NN-name",
  "review": "Freeform review text with findings, suggestions, etc."
}
```

The review is freeform prose - the subagent has flexibility in how it structures its feedback.

### 6. Write Review to File

Write the subagent's review to `{code_review_dir}/section-NN-review.md`:

```markdown
# Code Review: Section NN - Name

{review text from subagent}
```

### 7. Wait for the Ollama Reviewers

If the background task hasn't finished when the Claude review is written, say so in one line ("waiting for Ollama reviewers…") and end the turn. Claude Code re-invokes the session with a completion notification when a background task exits — no polling, no `sleep` loops, no user input. Then read `{code_review_dir}/section-NN-ollama-summary.json`.

If the summary is missing (the background shell died with the session, or `/clear`), re-run step 4 **once** before triage. If it is still missing after that (the script couldn't write it, e.g. bad arguments), use the summary JSON the background task printed to stdout; if that is empty too, record `ollama: ERROR (no summary)` and continue. Known risk, not locked against: a context reset while a run is still going can start a second run on the same GPU.

After the reviews are in, proceed to [code-review-interview.md](code-review-interview.md) for the interactive interview process.

See `agents/code-reviewer.md` for the custom subagent definition.
