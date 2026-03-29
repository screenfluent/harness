# Context Management Roadmap

Phased approach to managing the context window in harness sessions.

## Problem

As sessions grow, tool results accumulate and dominate context size.
A `read_file` from 30 turns ago still sits in the payload at full size
even though the file has since been modified. Every turn re-sends
the entire history verbatim.

## Phases

### Phase 1: Trim old tool results (simple hook, zero LLM)

Hook `15-context` in `assemble/`. Runs between `10-messages` and `20-tools`.

Logic:
- Last N messages (e.g. 10 turns = ~20 user+assistant messages) → **verbatim** with full tool results
- Older messages → tool results **replaced** with one-line summaries:
  - `read_file src/lib/db/schema.ts` → `[read file: src/lib/db/schema.ts, 88 lines]`
  - `bash npm test` → `[ran: npm test, exit 0, 45 lines output]`
  - `write_file src/lib/foo.ts` → `[wrote: src/lib/foo.ts, 120 bytes]`
  - `str_replace src/lib/foo.ts` → `[edited: src/lib/foo.ts, 3 edits]`

Model sees **what** the agent did, but doesn't get 500 lines of output from 30 turns ago.

**Estimate:** ~40-50 LOC bash. Zero dependencies. Zero LLM calls.

### Phase 2: Token counter + budget (info in system prompt)

Add context usage info to the payload:
- `[Context: 45k/200k tokens. 12 messages, 8 tool results summarized.]`
- Model knows how much room it has and can decide whether to read full files

**Estimate:** ~20 LOC bash (simple `wc -c * 0.25` as token approximation).

### Phase 3: Sliding window with summarization (LLM side-call)

When context exceeds ~60% of model limit:
- Take the oldest 50% of messages
- Side-channel LLM call: "summarize this conversation in 500 words"
- Insert summary as the first message
- Drop originals

**Estimate:** ~60 LOC bash + one `curl` to API.

### Phase 4: Smart context (RAG on history)

Instead of chronological — select messages **relevant** to the current
question. Embedding search on session history.

Far future. Likely requires python.

## Architecture note

This is possible because harness assembles context from files on every
turn (see Greg's tweet thread, Mar 26 2026). The `10-messages` hook is
a program that builds context — it doesn't have to load messages
verbatim, in order, or in total. Each phase replaces or wraps this
program with progressively smarter context selection.
