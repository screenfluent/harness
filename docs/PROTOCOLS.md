# Plugin Protocols

Harness has three plugin types that follow executable protocols: tools, providers, and hooks. Each is a standalone executable (any language) discovered from `.harness/` directories and `plugins/*/`.

## Tools

Tools are executables in `tools/` directories. They respond to three flags:

| Flag | Input | Output | Purpose |
|------|-------|--------|---------|
| `--schema` | — | JSON | Tool definition (name, description, input_schema) |
| `--describe` | — | one line on stdout | Short description for `hs tools` and `hs help` |
| `--exec` | JSON on stdin | text on stdout | Execute the tool with the given input |

### `--schema`

Returns a JSON object matching the Anthropic tool schema format:

```json
{
  "name": "tool_name",
  "description": "What this tool does",
  "input_schema": {
    "type": "object",
    "properties": {
      "param": { "type": "string", "description": "..." }
    },
    "required": ["param"]
  }
}
```

### `--exec`

Receives `input_schema`-shaped JSON on stdin. Stdout becomes the tool result sent back to the model. Stderr goes to `HARNESS_LOG`. A non-zero exit marks the result as `is_error: true`.

### Environment

Tools receive the standard hook environment (see below) plus:
- `HARNESS_CWD` — the session's original working directory (tools should `cd` here)
- `HARNESS_TOOL_TIMEOUT` — optional timeout in seconds

### Example

```bash
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --schema)   cat <<'JSON'
{ "name": "greet", "description": "Say hello",
  "input_schema": {"type":"object","properties":{"name":{"type":"string"}},"required":["name"]} }
JSON
    ;;
  --describe) echo "Say hello to someone" ;;
  --exec)
    name="$(jq -r '.name' < /dev/stdin)"
    echo "Hello, ${name}!"
    ;;
esac
```

## Providers

Providers are executables in `providers/` directories. They handle API communication with an LLM backend.

### Execution mode

Stdin: assembled payload JSON (with `model`, `system`, `messages`, `tools` fields).
Stdout: raw API response.
Stderr: errors only.

Non-zero exit signals a fatal error.

### Introspection flags

All flags are optional — a provider that only handles stdin/stdout still works. But supporting these flags enables auto-discovery and dynamic help.

| Flag | Output | Purpose |
|------|--------|---------|
| `--describe` | one line on stdout | Short description for `hs help` |
| `--ready` | exit code only | Exit 0 if credentials are configured, 1 if not |
| `--defaults` | `key=value` lines | Default values for HARNESS_ vars (e.g. `model=...`) |
| `--env` | text lines | Env vars this provider supports, with descriptions |

### `--ready`

Used for auto-selection. Harness iterates discovered providers (sorted by name) and picks the first where `--ready` exits 0. This check should be fast — just test whether the required env var is set, don't make network calls.

```bash
[[ "${1:-}" == "--ready" ]] && { [[ -n "${MY_API_KEY:-}" ]]; exit $?; }
```

### `--defaults`

Key-value pairs, one per line. Harness uses these to populate unset `HARNESS_` vars after auto-selecting a provider. Currently recognized keys:

| Key | Maps to |
|-----|---------|
| `model` | `HARNESS_MODEL` |

```bash
[[ "${1:-}" == "--defaults" ]] && { echo "model=claude-sonnet-4-20250514"; exit 0; }
```

### `--env`

Freeform text listing supported env vars. Displayed in `hs help` under each provider. Convention: `VAR_NAME` followed by spaces and a description, with defaults in parentheses.

```
MY_API_KEY     API key (required)
MY_API_URL     API endpoint (https://api.example.com/v1)
MY_MAX_TOKENS  max response tokens (8192)
```

### Example

```bash
#!/usr/bin/env bash
set -euo pipefail

[[ "${1:-}" == "--describe" ]] && { echo "Example LLM API"; exit 0; }
[[ "${1:-}" == "--ready" ]]    && { [[ -n "${EXAMPLE_KEY:-}" ]]; exit $?; }
[[ "${1:-}" == "--defaults" ]] && { echo "model=example-v1"; exit 0; }
[[ "${1:-}" == "--env" ]]      && { cat <<'EOF'
EXAMPLE_KEY       API key (required)
EXAMPLE_URL       API endpoint (https://api.example.com/v1)
EOF
exit 0; }

EXAMPLE_KEY="${EXAMPLE_KEY:?EXAMPLE_KEY not set}"
payload="$(cat)"
# ... build request, call API, output response ...
```

## Hooks

Hooks are executables in `hooks.d/<stage>/` directories. They form a pipeline: each hook's stdout feeds the next hook's stdin. Named `NN-name` where `NN` is a two-digit sort key controlling execution order.

### Pipeline behavior

1. Hooks are collected from all plugin sources (lowest to highest priority)
2. Deduplicated by basename — a local `10-save` overrides a bundled `10-save`
3. Sorted by basename (numeric prefix determines order)
4. Executed as a chain: stdin of hook N = stdout of hook N-1
5. Non-zero exit aborts the chain and returns the error to the caller

### Stages

| Stage | Stdin | When | Purpose |
|-------|-------|------|---------|
| `on-start` | empty | session begins | initialize session env |
| `assemble` | `{}` | before each API call | build the request payload (messages, tools, prompts) |
| `receive` | API response JSON | after API response | save assistant message, display output |
| `pre-tool` | tool_use JSON | before tool execution | approve/reject/modify tool calls |
| `tool-done` | tool result JSON | after tool execution | save tool results |
| `on-error` | API response JSON | on API error | error handling |
| `on-end` | empty | session ends | cleanup |

### Environment

Every hook receives:

| Variable | Contents |
|----------|----------|
| `HARNESS_SESSION` | path to current session directory |
| `HARNESS_STAGE` | current hook stage name |
| `HARNESS_MODEL` | selected model |
| `HARNESS_PROVIDER` | selected provider name |
| `HARNESS_SYSTEM` | base system prompt |
| `HARNESS_ROOT` | harness installation directory |

### `pre-tool` specifics

Receives the tool_use block as JSON on stdin:

```json
{"type": "tool_use", "id": "toolu_...", "name": "bash", "input": {"command": "ls"}}
```

Exit 0 to allow execution, non-zero to block it.

### `tool-done` specifics

Receives a JSON object on stdin:

```json
{
  "tool_use_id": "toolu_...",
  "name": "bash",
  "input": {"command": "ls"},
  "result": "file1\nfile2\n",
  "is_error": false
}
```

### Example

```bash
#!/usr/bin/env bash
# hooks.d/pre-tool/50-confirm — ask before running bash commands
set -euo pipefail

tc="$(cat)"
name="$(echo "${tc}" | jq -r '.name')"

if [[ "${name}" == "bash" ]]; then
  cmd="$(echo "${tc}" | jq -r '.input.command')"
  read -p "run: ${cmd}? [y/N] " -r reply </dev/tty
  [[ "${reply}" =~ ^[Yy] ]] || exit 1
fi

echo "${tc}"
```

## Discovery order

Plugin sources are searched lowest to highest priority:

1. Bundled plugins (`<harness-root>/plugins/*/`, sorted)
2. For each `.harness/` dir from global (`~/.harness`) to local (CWD):
   - Plugin packs within that dir (`plugins/*/`, sorted)
   - The dir itself

Within each type (tools, providers, hooks), later entries override earlier ones sharing the same basename. This means a local plugin always overrides a bundled one with the same name.
