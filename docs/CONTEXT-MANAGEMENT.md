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

```
Your message →
  ├─ Last 10 user messages + their tool calls/results → VERBATIM
  ├─ Old observed messages → observation.md (LLM compression)
  ├─ Old unobserved delta (< batch) → Phase 1 (tool results trimmed)
  └─ Delta ≥ batch → observer appends to observation.md
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
   observer compresses all of them
2. **Incremental**: tracks observed count in `state.json`. Only new
   (unobserved) messages are sent to the observer as delta
3. **Batching**: waits until ≥ batch (5) new user messages accumulate.
   Between batches, Phase 1 trims the delta's tool results
4. **Append**: existing observation is passed as context so the observer
   appends coherently. Output is the complete updated observation
5. **Cache**: if nothing new → zero API call, instant cache hit

### Storage

```
sessions/<id>/observations/
  observation.md    # cumulative observation log
  state.json        # {"observed_user_msgs": N}
```

### Observation format

Plain text with role prefixes and emoji priorities:

```
Date: 2026-03-29

- 🔴 User: Test file editing on the tailwindgallery.com project
- 🟡 Agent: Explored project structure, read config files:
    → package.json (68 lines): tailwindgallery v0.0.1, type: module
    → svelte.config.js (30 lines): adapter-node, async: true,
      remoteFunctions: true, CSP mode: 'auto'
- 🟡 Agent: Read database schema:
    → schema.ts (88 lines): PostgreSQL/Drizzle. sites table with
      slug varchar(160), regex check, siteStatus enum, analyzerData jsonb.
      authSessions table: tokenHash, expiresAt, revokedAt.
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

When observation.md grows large, a reflector LLM compresses it into
a higher-level summary. Hierarchical:

```
Recent messages (verbatim)
  ↑ Phase 1: tool results trimmed
Observations (~40 lines per batch)
  ↑ Phase 2: observer LLM
Reflections (~10 lines per observation batch)
  ↑ Phase 3: reflector LLM
```

## Phase 4: Retrieval (future)

Agent gets a `recall` tool to page through raw messages behind any
observation. When the compressed summary isn't enough, agent reads
the originals.
