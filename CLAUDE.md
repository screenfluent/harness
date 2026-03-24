# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Harness is a minimal agent loop in bash. The core script (~550 lines) handles plugin discovery, hook dispatch, and the agentic loop. Everything else — tools, providers, prompt loading, message serialization, cost tracking, approval gates — lives in plugins that can be written in any language.

Dependencies: bash 4+, jq, curl. No package manager, no language runtime.

## Commands

```bash
# Run the agent
bin/harness run "do something"      # one-shot
bin/harness chat                     # interactive REPL
bin/harness chat <session-id>        # resume session

# Inspect
bin/harness tools                    # list discovered tools
bin/harness hooks [stage]            # list discovered hooks
bin/harness session list             # list sessions
bin/harness session show <id>        # inspect session

# There is no test suite or linter.
# Verify changes by running the agent and inspecting session files.
```

The `bin/hs` symlink is an alias for `bin/harness`.

## Architecture

### The Loop

`assemble → send → receive → (execute tools → save results → repeat) → done`

The core loop in `bin/harness` orchestrates this. It does not implement message assembly, tool execution, prompt loading, or response saving — hooks do all of that.

### Plugin Discovery

Harness walks from CWD upward to `/`, collecting `.harness/` directories. Local overrides global by basename. Core plugins in `plugins/core/` are always loaded last (lowest priority).

### Four Plugin Types

**Tools** (`tools/`): Executables responding to `--schema`, `--describe`, `--exec`. Input is JSON on stdin via `--exec`, output on stdout. Language-agnostic. Core tools: `bash`, `read_file`, `write_file`, `str_replace`, `list_dir`.

**Hooks** (`hooks.d/<stage>/`): Pipeline executables named `NN-name` (numeric prefix for sort order). Each hook's stdout feeds the next's stdin. Non-zero exit aborts the chain. Stages: `on-start`, `assemble`, `pre-tool`, `receive`, `tool-done`, `on-error`, `on-end`.

**Providers** (`providers/`): Receive assembled payload JSON on stdin, output raw API response. Support introspection flags: `--describe`, `--ready`, `--defaults`, `--env`. If `HARNESS_PROVIDER` is not set, harness auto-selects the first provider whose `--ready` exits 0. Built-in: `anthropic`, `zai`.

**Prompts** (`HARNESS.md` + `prompts/*.md`): Concatenated into the system prompt by the `30-prompts` assemble hook. Global first, local last.

### Session State is Filesystem

Session storage is auto-discovered: harness walks from CWD upward looking for the first `.harness/sessions/` directory. If none is found, it falls back to `~/.harness/sessions/`. Create `<project>/.harness/sessions/` to keep session history local to a repo. Override with `HARNESS_SESSIONS`.

Sessions live in `<sessions-dir>/<id>/messages/` as numbered markdown files with YAML frontmatter. The `10-messages` assemble hook reconstructs the API messages array from these files. Tool calls within assistant messages are stored as fenced code blocks with `tool_use` info strings.

### Key Files

- `bin/harness` — entire core: CLI, discovery, loop, tool execution
- `plugins/core/hooks.d/assemble/` — payload construction (messages, tools, prompts)
- `plugins/core/hooks.d/receive/10-save` — response persistence
- `plugins/core/hooks.d/tool-done/10-save` — tool result persistence
- `plugins/core/providers/anthropic` — API call (curl + jq)
- `plugins/core/tools/` — five built-in tools

## Conventions

- All bash scripts use `set -euo pipefail` and `local` variables
- All JSON manipulation goes through `jq` — no bash JSON parsing
- Tools and hooks are executable files, not sourced scripts
- Hook naming: `NN-name` where NN is a two-digit sort key
- Tool protocol: `--schema` (JSON), `--describe` (one-line), `--exec` (JSON stdin → stdout)
- Provider protocol: `--describe`, `--ready`, `--defaults`, `--env`, plus stdin→stdout for execution
- Full protocol docs in `docs/PROTOCOLS.md`
- `HARNESS_CWD` tracks the session's original working directory; tools use it
