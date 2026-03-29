# Context Management

Phased approach to managing the context window in harness sessions.
Inspired by Mastra's Observational Memory (94.87% on LongMemEval).

## Problem

As sessions grow, tool results accumulate and dominate context size.
A `read_file` from 30 turns ago still sits in the payload at full size
even though the file has since been modified. Every turn re-sends
the entire history verbatim.

## Architecture

Harness assembles context from files on every turn. The `10-messages`
hook is a program that builds the messages array — it doesn't have to
load messages verbatim, in order, or in total. Context management
hooks transform the payload between `10-messages` and `20-tools`.

Files on disk are never modified. Compression is payload-only.

## Phase 1: Trim old tool results ✅

Hook `15-context` in `assemble/`. Runs between `10-messages` and
`20-tools`.

- Last 10 turns → verbatim with full tool results
- Older tool results (>200 chars) → one-line summaries:
  - `[read: src/db/schema.ts, 88 lines]`
  - `[ran: npm test, 45 lines output]`
  - `[edited: config.ts, 3 edits]`
  - `[wrote: src/foo.ts, 120 bytes]`
  - `[listed: src/lib, 74 lines]`

Zero LLM calls. ~100 LOC python inside bash hook.

## Phase 2: Observation — compress old turns into a log

When enough turns accumulate beyond the Phase 1 window, an LLM
side-call compresses them into an observation log. The goal is a
dense, readable record of what happened.

### Observation format

Plain text, not structured data. Optimized for LLM comprehension
and human debugging. Inspired by Mastra's Observational Memory
(94.87% on LongMemEval).

Key principles:
- **Knowledge, not metadata** — don't write "read schema.ts, 88 lines".
  Write what was IN the file: tables, columns, constraints, patterns.
- **Emoji priority** — 🔴 critical/blocking, 🟡 notable, ✅ completed
- **Nested `→` for file discoveries** — indented under the action
- **User intent preserved** — what they asked, in their words
- **Errors and workarounds captured** — future self needs these

Example (from a real harness testing session):

```
Date: 2026-03-29

- 🔴 (14:03) User asked to test file editing capabilities on the
  tailwindgallery.com project
- 🟡 (14:03) Agent explored project structure:
    → listed root dir (74 entries): SvelteKit project with Drizzle,
      src/lib/features/ pattern, Bun package manager
    → package.json (68 lines): tailwindgallery v0.0.1, type: module,
      deps: sveltekit, drizzle-orm, valibot, postgres
- 🟡 (14:05) Agent tested str_replace on package.json:
    → changed version 0.0.1 → 1.3.37, verified, reverted
    → str_replace had perl quoting bug with special chars — switched
      to python with env vars as workaround
- 🟡 (14:08) Agent read project config files:
    → svelte.config.js (30 lines): adapter-node, experimental async,
      remoteFunctions enabled, CSP mode: 'auto'
    → drizzle.config.ts (22 lines): Drizzle Kit config, verbose: true
- 🔴 (14:10) Agent read database schema:
    → schema.ts (88 lines): PostgreSQL schema with Drizzle ORM.
      sites table: slug (varchar 160, unique, regex-checked),
      name (varchar 120), targetUrl (varchar 2048), siteStatus
      enum (draft/published), publishedAt, screenshotData (jsonb).
      authSessions table: tokenHash, expiresAt, revokedAt.
      Check constraint: slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
- 🟡 (14:12) Agent read rate-limit.ts (47 lines): in-memory
    fixed-window rate limiter. Map<string, {count, resetAt}>,
    sweep every 100 checks. Returns boolean from check().
- ✅ (14:15) Agent tested multi-edit on svelte.config.js:
    → 3 simultaneous edits (mode, async, remoteFunctions), verified,
      reverted. Multi-edit atomic validation working.
```

### What matters

- **What the user asked** — intent, not verbatim transcript
- **What the agent did** — tools used, files touched, outcomes
- **What was discovered** — schemas, APIs, patterns, config values,
  constraints. This is the most valuable part. Future turns need
  this knowledge without re-reading the files.
- **What went wrong** — errors, bugs, workarounds

### What doesn't matter (yet)

- Token counting precision (simple turn count triggers are enough)
- Structured output / JSON (text is the universal interface)
- Exact thresholds (tuning comes after the format is proven)

### Implementation sketch

A new hook or extension to `15-context`:
1. Count turns beyond the keep window
2. When enough accumulate (e.g. 10+ old turns), make one LLM
   side-call to compress them into the observation format above
3. Store the observation as a file: `sessions/<id>/observations/L1-001.md`
4. On next assemble, inject observation text as the first message
   instead of the raw old messages

The observation file is append-only — new compressions add new
sections. Raw messages on disk stay untouched.

## Phase 3: Reflection — compress observations

When observations grow large, a second LLM call compresses them
into higher-level reflections. This is hierarchical:

```
Recent messages (verbatim, last 10 turns)
  ↑ Phase 1: tool results trimmed
Observations (compressed turns, ~500 words per 10 turns)  
  ↑ Phase 2: LLM side-call
Reflections (compressed observations, ~200 words per batch)
  ↑ Phase 3: LLM side-call
```

Multiple observation batches merge into one reflection.
Multiple reflections could merge further if sessions get very long.

Same file-based approach:
```
sessions/<id>/
  messages/              # raw messages (source of truth)
  observations/
    L1-001.md            # turns 1-10 compressed
    L1-002.md            # turns 11-20 compressed
  reflections/
    L2-001.md            # L1-001 + L1-002 compressed together
```

## Phase 4: Retrieval (future)

Agent gets a `recall` tool to page through raw messages behind
any observation. When the compressed summary isn't enough, agent
reads the originals. Similar to Mastra's retrieval mode.

## Priority

Phase 1 is done and works. Phase 2 is the next step — getting the
observation format right matters more than optimizing when it fires.
Phases 3-4 are for when sessions routinely exceed hundreds of turns.
