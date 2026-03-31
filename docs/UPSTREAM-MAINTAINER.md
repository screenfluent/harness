# Upstream maintainer profile

Notes on Greg Wedow's (@gregwedow) coding style and preferences,
learned from PR reviews and upstream commits. Reference this before
submitting PRs to `wedow/harness`.

## Code style

- **Minimal diffs.** A 4-line fix beats a 280-line refactor. If the
  core fix is 2 lines, submit 2 lines — not 2 lines plus locking,
  traps, validation, and cleanup.
- **No new dependencies.** Rejected our python callback server.
  Adapted the existing netcat pattern from chatgpt provider instead.
  Harness dependencies are: bash 4+, jq, curl. That's it.
- **Reuse existing patterns.** When he needed a callback server for
  claude, he copied the FIFO+netcat approach from chatgpt — not
  a new solution. Look at what already exists before inventing.
- **Comments are sparse.** Short inline comments, no section headers
  with box-drawing or `# --- label ---` decorators. The code should
  be self-explanatory. Exception: the login hook uses `# --- label ---`
  for major sections (constants, PKCE, helpers, etc.).
- **One-liners with `[[ ]] && { ...; }`.** Not `if/fi` blocks for
  simple guards.
- **No over-engineering.** No dataclasses, no enums, no strategy
  patterns. Bash scripts stay imperative and linear.

## Architecture preferences

- **Thin wrappers over duplication.** Claude provider delegates to
  anthropic provider instead of reimplementing the API call. OAuth
  is the only unique part; everything else is shared.
- **Hooks do one thing.** Each hook is focused. The assemble pipeline
  is `10-messages → 20-tools → 25-skills → 30-prompts`. Context
  management (our `15-context`) fits naturally because it's a single
  hook in the pipeline.
- **Provider variants over provider plugins.** For OpenAI-compatible
  APIs, a `.conf` file is preferred over a new provider directory.
- **Files are the database.** Session state is markdown files on disk.
  Auth is JSON files. No sqlite, no process-level state.

## What he accepts

- Bug fixes with clear root cause and minimal diff.
- Fixes that match how the reference implementation (pi/Node.js) does it,
  when you can cite the specific approach.
- Improvements to his own code (he merged our error display PR #4 and
  our frontmatter parser fix PR #3 without changes).
- Netcat/bash solutions over python/node solutions.

## What he rejects

- Large PRs that refactor beyond what's needed for the fix.
- New runtime dependencies (python, node).
- Defensive code for theoretical problems without evidence
  (locking, cleanup traps, single-read patterns).
- Code that's technically better but harder to read.

## PR submission checklist

1. **Is the diff under 10 lines?** If not, can it be split?
2. **Does it use only bash, jq, curl?** No python, no node.
3. **Does it reuse an existing pattern from the codebase?**
4. **Can you explain the bug in one sentence?**
5. **Does the PR description cite evidence?** (reference impl,
   specific failure scenario, not theoretical concerns)
6. **Is the commit message one line + optional body?** His style:
   `fix: short description` with a body explaining why.

## Communication style

- Responsive on Twitter DMs/replies. Merges fast when he agrees.
- Will take your idea and reimplement it his way rather than merge
  a PR that doesn't match his style (our OAuth PR #5).
- Credits contributions — merged our PRs and built on our root cause
  discovery (Cloudflare User-Agent).
- "Going to throw in a small fix" = he'll do it himself within hours.

## History

| PR | Outcome | Lesson |
|----|---------|--------|
| #1 netcat fix | Merged | Small, clear bug fix — accepted as-is |
| #2 SSE streaming | Closed | Upstream refactored claude→anthropic delegation, making our streaming redundant |
| #3 frontmatter + empty blocks | Merged | Bug fix with clear reproduction — accepted |
| #4 error display in REPL | Merged | 4 lines, obvious gap in functionality |
| #5 OAuth overhaul (280 lines) | Rejected | Too large, python dependency, over-engineered. He took the root cause (User-Agent) and did his own minimal fix |
| #6 expiry margin | Pending | 2 lines, cites pi reference. High confidence |
| #7 atomic writes | Pending | 4 lines, well-known idiom. Medium-high confidence |
