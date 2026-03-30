# Fork: screenfluent/harness

Fork of [wedow/harness](https://github.com/wedow/harness) with context
management, UX improvements, and bug fixes.

Upstream: `git remote add upstream https://github.com/wedow/harness.git`

## What this fork adds

### Context management (`plugins/core/hooks.d/assemble/15-context`)

Two-tier context compression so sessions don't overflow the context window.

**Phase 1 — tool result trimming (zero LLM):**
Old tool results (>200 chars) are replaced with one-line summaries like
`[read: schema.ts, 88 lines]`. Runs as fallback or between observer batches.

**Phase 2 — observer compression (Gemini Flash):**
When enough old user messages accumulate, an LLM side-call compresses them
into a dense observation log. Incremental — only new messages are sent.
Batched — waits for N new user messages before calling the observer.

Architecture: `parse → decide → execute → assemble → normalize → output`.
Decision logic is a pure function. Single exit point. Prompts in external
`.md` files.

Storage: immutable batch files (`obs-0001-u0001-u0008.md`) + `index.json`
with per-batch metadata (user range, model, timestamp).

See `docs/CONTEXT-MANAGEMENT.md` for full documentation.

Config:
```
HARNESS_CONTEXT_KEEP_MSGS    recent user messages to keep verbatim (default: 10)
HARNESS_OBSERVER_KEY         Gemini API key (required for Phase 2)
HARNESS_OBSERVER_MODEL       observer model (default: gemini-3-flash-preview)
HARNESS_OBSERVER_THRESHOLD   old user messages needed for first observation (default: 5)
HARNESS_OBSERVER_BATCH       new user messages to batch before observing (default: 5)
```

### Claude OAuth fixes (`plugins/anthropic/`)

Upstream's OAuth login failed due to Cloudflare blocking `curl` User-Agent
on `platform.claude.com`. This fork:

- Sends `User-Agent: node` on all token endpoint calls
- Implements local HTTP callback server on `localhost:53692` (auto-captures
  the OAuth redirect instead of manual code paste)
- Adds 5-minute expiry margin, mkdir-based locking for concurrent refresh,
  atomic cache reads/writes, retry on transient failures
- Replaces OAuth credentials on re-login instead of appending stale tokens

Submitted upstream as PR #5.

### Message assembly fixes (`plugins/anthropic/hooks.d/assemble/10-messages`)

- Frontmatter parser: `---` in assistant response body no longer truncates
  the message (was silently dropping tool_use blocks)
- Empty text blocks filtered before API call

Merged upstream via PR #1 and #3.

### Error display (`plugins/core/hooks.d/error/10-display`)

Error messages now display in the streaming REPL. Previously, errors were
only written to `.output` which the REPL ignores in streaming mode —
user saw `hook exited 1` with no explanation.

Submitted upstream as PR #4.

### REPL improvements (`plugins/core/commands/agent`)

- Session history displayed on resume
- Multiline input with `\` continuation
- Whitespace-only input skipped
- Session info (model, provider, session ID) shown on start
- Skills deduped and tools sorted in startup display

### Tool improvements

- `str_replace`: multi-edit support (`edits[]` array), diff-style display,
  python-based replacement (fixes perl escaping bugs)
- `read_file`: auto-truncation at 2000 lines with offset/limit pagination
- Compact, colored tool execution display with box-drawing characters

### Auth storage (`plugins/auth/commands/auth`)

- OAuth credentials replace instead of append
- Atomic writes via `mktemp` + `mv`
- Index validation on `auth rm`

## Upstream PRs

| PR | Status | Description |
|----|--------|-------------|
| #1 | Merged | macOS BSD netcat compatibility in ChatGPT OAuth |
| #3 | Merged | Frontmatter parser + empty text block fix |
| #4 | Open   | Error display in streaming REPL |
| #5 | Open   | Complete Claude OAuth fix |

## Syncing with upstream

```bash
git fetch upstream
git merge upstream/master
# Resolve conflicts if any, test, push
```
