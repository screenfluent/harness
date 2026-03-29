"""
15-context.py — Two-tier context compression for harness.

Phase 1: Replace old tool_result content with one-line summaries.
Phase 2: When enough old turns exist, call observer LLM to compress
         them into a dense observation log. Cache the result on disk.

Reads JSON payload from stdin, writes modified JSON to stdout.
Logs to stderr.
"""
import json, os, sys, subprocess, hashlib, glob

# ── Config ──────────────────────────────────────────────────────────
keep_turns    = int(os.environ.get("KEEP_TURNS", "10"))
session_dir   = os.environ.get("SESSION_DIR", "")
observer_model = os.environ.get("OBSERVER_MODEL", "gemini-3-flash-preview")
observer_key  = os.environ.get("OBSERVER_KEY", "")
observer_threshold = int(os.environ.get("OBSERVER_THRESHOLD", "5"))

payload  = json.loads(sys.stdin.read())
messages = payload.get("messages", [])

if not messages:
    print(json.dumps(payload))
    sys.exit(0)

# ── Find the keep boundary ──────────────────────────────────────────
# Count real user turns (string content) from the end.
turn_count = 0
keep_from = 0

for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]
    if msg.get("role") == "user" and isinstance(msg.get("content"), str):
        turn_count += 1
        if turn_count >= keep_turns:
            keep_from = i
            break

if keep_from == 0:
    print(json.dumps(payload))
    sys.exit(0)

# ── Phase 1: Trim old tool results ─────────────────────────────────
# Build map: tool_use_id → {name, input} from old assistant messages
tool_use_map = {}
for i in range(0, keep_from):
    msg = messages[i]
    if msg.get("role") != "assistant":
        continue
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    for block in content:
        if block.get("type") == "tool_use":
            tool_use_map[block["id"]] = {
                "name": block.get("name", "unknown"),
                "input": block.get("input", {})
            }

def summarize_tool(tool_use_id, result_content):
    info = tool_use_map.get(tool_use_id, {"name": "unknown", "input": {}})
    name = info["name"]
    inp = info["input"]
    lines = result_content.count("\n") + 1 if result_content else 0

    if name == "read_file":
        return f"[read: {inp.get('path', '?')}, {lines} lines]"
    elif name == "write_file":
        return f"[wrote: {inp.get('path', '?')}, {len(inp.get('content', ''))} bytes]"
    elif name == "str_replace":
        path = inp.get("path", "?")
        edits = inp.get("edits")
        return f"[edited: {path}, {len(edits)} edits]" if edits else f"[edited: {path}]"
    elif name == "bash":
        cmd = inp.get("command", "?")
        if len(cmd) > 60: cmd = cmd[:60] + "..."
        return f"[ran: {cmd}, {lines} lines output]"
    elif name == "list_dir":
        return f"[listed: {inp.get('path', '?')}, {lines} lines]"
    elif name == "agent":
        prompt = inp.get("prompt", "?")
        if len(prompt) > 60: prompt = prompt[:60] + "..."
        return f"[subagent: {prompt}]"
    else:
        return f"[{name}: {lines} lines]"

trimmed_count = 0
for i in range(0, keep_from):
    msg = messages[i]
    if msg.get("role") != "user":
        continue
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    for j, block in enumerate(content):
        if block.get("type") == "tool_result":
            original = block.get("content", "")
            if len(original) > 200:
                tid = block.get("tool_use_id", "")
                messages[i]["content"][j]["content"] = summarize_tool(tid, original)
                trimmed_count += 1

if trimmed_count > 0:
    print(f"[context] phase 1: trimmed {trimmed_count} old tool results", file=sys.stderr)

# ── Phase 2: Observer — compress old turns into observation ─────────
# Count old real user turns (before keep_from)
old_turn_count = 0
for i in range(0, keep_from):
    msg = messages[i]
    if msg.get("role") == "user" and isinstance(msg.get("content"), str):
        old_turn_count += 1

# Check if we should observe
should_observe = (
    old_turn_count >= observer_threshold
    and observer_key
    and session_dir
)

if not should_observe:
    payload["messages"] = messages
    print(json.dumps(payload))
    sys.exit(0)

# ── Check cache: have we already observed these turns? ──────────────
obs_dir = os.path.join(session_dir, "observations")
os.makedirs(obs_dir, exist_ok=True)

# Hash the old messages to detect if they changed since last observation
old_msgs_json = json.dumps(messages[:keep_from], sort_keys=True)
old_hash = hashlib.sha256(old_msgs_json.encode()).hexdigest()[:16]
obs_file = os.path.join(obs_dir, f"obs-{old_hash}.md")

if os.path.exists(obs_file):
    # Use cached observation
    with open(obs_file) as f:
        observation = f.read()
    print(f"[context] phase 2: using cached observation ({old_turn_count} turns)", file=sys.stderr)
else:
    # Build transcript for the observer
    transcript_lines = []
    for i in range(0, keep_from):
        msg = messages[i]
        role = msg.get("role", "?").upper()
        content = msg.get("content", "")

        if isinstance(content, str):
            if not content.strip():
                continue
            transcript_lines.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type", "")
                if btype == "text" and block.get("text", "").strip():
                    transcript_lines.append(f"[{role}]: {block['text']}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    inp_str = json.dumps(inp)
                    if len(inp_str) > 300:
                        inp_str = inp_str[:300] + "..."
                    transcript_lines.append(f"[{role} tool_call]: {name}({inp_str})")
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    tc = block.get("content", "")
                    if len(tc) > 500:
                        tc = tc[:500] + f"... [{len(tc)} chars total]"
                    info = tool_use_map.get(tid, {"name": "?", "input": {}})
                    transcript_lines.append(f"[TOOL_RESULT ({info['name']})]: {tc}")

    transcript = "\n".join(transcript_lines)

    # Observer prompt
    prompt = f"""Compress the transcript below into an observation log. Output the log directly — no thinking, no preamble, no explanation.

Format:

Date: today

- 🔴 User: [what human asked/decided — translate Polish to English]
- 🟡 Agent: [what agent did in response]
    → file.ts (N lines): what was IN the file — schemas, values, patterns
- ✅ Outcome: [verified result]

Rules:
- ALWAYS prefix with "User:", "Agent:", or "Outcome:"
- Capture KNOWLEDGE in files (schemas, config values, constraints), not just "read file X"
- Capture errors and who resolved them
- Use → for file discoveries, indented under agent action
- Group read-edit-verify into single entries
- Skip greetings/chitchat
- Omit timestamps
- 30-50 lines total, English

---
{transcript}"""

    # Call Gemini API
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{observer_model}:generateContent?key={observer_key}"
    api_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    })

    try:
        result = subprocess.run(
            ["curl", "-s", api_url,
             "-H", "Content-Type: application/json",
             "-d", api_body],
            capture_output=True, text=True, timeout=30
        )
        resp = json.loads(result.stdout)
        parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        observation = ""
        for p in parts:
            if "text" in p and not p.get("thought"):
                observation += p["text"]

        if not observation.strip():
            raise ValueError("Empty observation from API")

        # Strip markdown code fences if present
        obs = observation.strip()
        if obs.startswith("```"):
            obs = "\n".join(obs.split("\n")[1:])
        if obs.endswith("```"):
            obs = "\n".join(obs.split("\n")[:-1])
        observation = obs.strip()

        # Save to cache
        with open(obs_file, "w") as f:
            f.write(observation)

        usage = resp.get("usageMetadata", {})
        in_tok = usage.get("promptTokenCount", "?")
        out_tok = usage.get("candidatesTokenCount", "?")
        print(f"[context] phase 2: observed {old_turn_count} turns → {len(observation)} chars ({in_tok} in, {out_tok} out tokens)", file=sys.stderr)

    except Exception as e:
        # Observation failed — fall back to Phase 1 only
        print(f"[context] phase 2: observer failed: {e}", file=sys.stderr)
        payload["messages"] = messages
        print(json.dumps(payload))
        sys.exit(0)

# ── Assemble final payload: observation + recent messages ───────────
# Replace old messages with a single user message containing the observation
obs_message = {
    "role": "user",
    "content": f"[Session history — compressed observation of earlier turns]\n\n{observation}"
}
# Need an assistant acknowledgment to keep alternation valid
obs_ack = {
    "role": "assistant",
    "content": "Understood. I have the compressed session history. Continuing from where we left off."
}

# New messages = observation + recent turns
payload["messages"] = [obs_message, obs_ack] + messages[keep_from:]
print(json.dumps(payload))
