# Context Management

Manages the context window in harness sessions. Two-tier compression
inspired by Mastra's Observational Memory.

## Problem

As sessions grow, tool results accumulate and dominate context size.
A `read_file` from 30 messages ago still sits in the payload at full
size even though the file has since been modified.

## Architecture

Hook `15-context` in `assemble/` transforms the payload between
`10-messages` and `20-tools`. Files on disk are never modified —
compression is payload-only.

Pipeline: `parse → decide → execute → assemble → normalize → output`

- `analyze_transcript()` splits messages into old/recent, builds tool map
- `build_plan()` is a **pure function** — chooses a Mode, no I/O
- `execute_plan()` runs the chosen strategy (trim, observe, or cached)
- `assemble()` builds the final message list (single place)
- `normalize_messages_for_api()` enforces user/assistant alternation (once)
- Single exit point in `main()`

Modes:

| Mode | When | What happens |
|------|------|-------------|
| PASSTHROUGH | nothing old | unchanged |
| TRIM_ALL | observer not configured or below threshold | Phase 1 on all old messages |
| CACHED | observation exists, no new messages | inject observation, zero API |
| TRIM_PENDING | delta < batch size | inject observation + Phase 1 on unobserved delta |
| OBSERVE | delta ≥ batch size | call observer on delta, save new batch |

```
Your message →
  ├─ Last 10 user messages + their tool calls/results → VERBATIM
  ├─ Observed messages → observation batches (LLM compression)
  ├─ Unobserved delta (< batch) → Phase 1 (tool results trimmed)
  └─ Delta ≥ batch → observer creates new batch file
```

## Config

| Env var | Default | What it does |
|---------|---------|-------------|
| `HARNESS_CONTEXT_KEEP_MSGS` | 10 | Recent user messages to keep verbatim |
| `HARNESS_OBSERVER_KEY` | — | Gemini API key (required for Phase 2) |
| `HARNESS_OBSERVER_MODEL` | gemini-3-flash-preview | Observer model |
| `HARNESS_OBSERVER_THRESHOLD` | 5 | Old user messages needed for first observation |
| `HARNESS_OBSERVER_BATCH` | 5 | New user messages to batch before observing |

"User messages" = messages typed by the human (string content), not
tool_result batches. Each starts a new interaction cycle.

## Phase 1: Trim old tool results ✅

When Phase 2 is disabled or delta < batch, old tool results (>200
chars) are replaced with one-line summaries:

- `[read: src/db/schema.ts, 88 lines]`
- `[ran: npm test, 45 lines output]`
- `[edited: config.ts, 3 edits]`

Zero LLM calls. Runs as fallback or on unobserved delta between
observer batches.

## Phase 2: Observer ✅

When enough old user messages accumulate, an LLM side-call (Gemini 3
Flash) compresses them into a dense observation log.

### How it works

1. **First observation**: when ≥ threshold (5) old user messages exist,
   observer compresses all of them into the first batch
2. **Incremental**: tracks observed count in `index.json`. Only new
   (unobserved) messages are sent to the observer as delta
3. **Batching**: waits until ≥ batch (5) new user messages accumulate.
   Between batches, Phase 1 trims the delta's tool results
4. **Batch files**: each observer call creates an immutable batch file.
   All batches are concatenated at assembly time
5. **Cache**: if nothing new → zero API call, existing batches reused

### Storage

```
sessions/<id>/observations/
  index.json                     # tracks batches and observed count
  obs-0001-u0001-u0008.md       # first batch (user messages 1-8)
  obs-0002-u0009-u0013.md       # second batch (user messages 9-13)
```

`index.json` example:
```json
{
  "last_observed_user_msg": 13,
  "batches": [
    {
      "id": 1,
      "file": "obs-0001-u0001-u0008.md",
      "user_range": [1, 8],
      "created_at": "2026-03-30T09:36:35",
      "model": "gemini-3-flash-preview"
    },
    {
      "id": 2,
      "file": "obs-0002-u0009-u0013.md",
      "user_range": [9, 13],
      "created_at": "2026-03-30T09:36:39",
      "model": "gemini-3-flash-preview"
    }
  ]
}
```

### Prompts

External files next to the hook:
- `15-context-observer.md` — initial observation prompt
- `15-context-observer-append.md` — incremental append prompt

### Observation format

Plain text with role prefixes and emoji priorities:

```
Date: 2026-03-29

- 🔴 User: Test file editing on the tailwindgallery.com project
- 🟡 Agent: Explored project structure, read config files:
    → package.json (68 lines): tailwindgallery v0.0.1, type: module
    → svelte.config.js (30 lines): adapter-node, CSP mode: 'auto'
- 🟡 Agent: Read database schema:
    → schema.ts (88 lines): PostgreSQL/Drizzle. sites table with
      slug varchar(160), siteStatus enum, analyzerData jsonb.
- 🔴 Agent: str_replace failed due to escaping — workaround via sed
- ✅ Outcome: Multi-edit atomic validation working, git diff clean
```

Key principles:
- **Knowledge, not metadata** — what was IN the file, not "read file X"
- **User intent** — what they asked, translated to English
- **Errors and workarounds** — future self needs these
- **Grouped** — read-edit-verify cycles as single entries

### Model comparison (tested on real 147-message session)

| Model | Lines | Chars | Compression | Quality |
|-------|-------|-------|-------------|---------|
| Gemini 2.5 Flash | 139 | 9.3k | 4x | verbose, repeats steps |
| **Gemini 3 Flash** | **43** | **3.2k** | **12x** | **best balance** |
| Gemini 3.1 Flash Lite | 30 | 2.0k | 19x | too aggressive, loses detail |

## Phase 3: Reflection (planned)

When observations grow large, a reflector LLM compresses them into
a higher-level summary. Batch files make this natural — reflector
targets specific batches and creates a separate reflection file.

```
sessions/<id>/
  observations/
    obs-0001-u0001-u0008.md
    obs-0002-u0009-u0013.md
  reflections/
    refl-0001-obs0001-obs0006.md
```

Assembly prefers reflection for older batches, raw observation for recent.

## Phase 4: Retrieval (future)

Agent gets a `recall` tool to page through raw messages behind any
observation. Batch metadata (`user_range` in `index.json`) maps
directly to source messages.
