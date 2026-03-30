"""
15-context.py — Context compression for harness sessions.

Pipeline: parse → decide → execute → assemble → normalize → output

Phase 1: Trim old tool results to one-line summaries (zero LLM).
Phase 2: Observer compresses old messages into observation batches (Gemini Flash).
         Incremental — only new messages are sent to the observer.
         Batched — waits for N new user messages before observing.

Reads JSON payload from stdin, writes modified JSON to stdout.
Logs decisions to stderr.
"""
import json, os, sys, subprocess, hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Config:
    keep_msgs: int = 10
    observer_threshold: int = 5
    observer_batch: int = 5
    observer_key: str = ""
    observer_model: str = "gemini-3-flash-preview"
    session_dir: str = ""

def load_config() -> Config:
    return Config(
        keep_msgs=int(os.environ.get("KEEP_MSGS", "10")),
        observer_threshold=int(os.environ.get("OBSERVER_THRESHOLD", "5")),
        observer_batch=int(os.environ.get("OBSERVER_BATCH", "5")),
        observer_key=os.environ.get("OBSERVER_KEY", ""),
        observer_model=os.environ.get("OBSERVER_MODEL", "gemini-3-flash-preview"),
        session_dir=os.environ.get("SESSION_DIR", ""),
    )


# ═══════════════════════════════════════════════════════════════════
# 2. DATA TYPES
# ═══════════════════════════════════════════════════════════════════

class Mode(Enum):
    PASSTHROUGH   = "passthrough"       # nothing to compress
    TRIM_ALL      = "trim_all"          # no observer, Phase 1 on everything old
    CACHED        = "cached"            # observation exists, no new messages
    TRIM_PENDING  = "trim_pending"      # delta < batch, Phase 1 on unobserved portion
    OBSERVE       = "observe"           # run observer on new delta

@dataclass
class ObservationBatch:
    id: int
    file: str
    user_start: int      # first user message index (1-based)
    user_end: int         # last user message index (1-based)

@dataclass
class ObservationIndex:
    last_observed_user_msg: int = 0
    batches: list = field(default_factory=list)  # list of ObservationBatch dicts

@dataclass
class TranscriptSlice:
    all_messages: list = field(default_factory=list)
    keep_from: int = 0                  # index: messages[:keep_from] are old
    old_user_count: int = 0             # user messages in old portion
    tool_use_map: dict = field(default_factory=dict)

@dataclass
class Plan:
    mode: Mode
    pending_start: int = 0              # message index where unobserved delta begins
    reason: str = ""

@dataclass
class ExecutionResult:
    observation_text: str = ""          # combined observation for injection
    pending_messages: list = field(default_factory=list)  # trimmed delta (between obs and recent)
    warnings: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════
# 3. PARSE — read input, analyze transcript, load store
# ═══════════════════════════════════════════════════════════════════

def find_keep_boundary(messages: list, keep_msgs: int) -> int:
    """Find the index where the 'keep' window starts (counting from end)."""
    count = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            count += 1
            if count >= keep_msgs:
                return i
    return 0

def count_user_messages(messages: list) -> int:
    return sum(
        1 for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    )

def build_tool_use_map(messages: list) -> dict:
    """Map tool_use IDs to their name+input for summarization."""
    result = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                result[block["id"]] = {
                    "name": block.get("name", "unknown"),
                    "input": block.get("input", {}),
                }
    return result

def analyze_transcript(messages: list, config: Config) -> TranscriptSlice:
    keep_from = find_keep_boundary(messages, config.keep_msgs)
    old_msgs = messages[:keep_from]
    return TranscriptSlice(
        all_messages=messages,
        keep_from=keep_from,
        old_user_count=count_user_messages(old_msgs),
        tool_use_map=build_tool_use_map(old_msgs),
    )

def find_pending_start(messages: list, keep_from: int, observed_count: int) -> int:
    """Find message index where unobserved messages begin."""
    if observed_count <= 0:
        return 0
    um_seen = 0
    for i in range(keep_from):
        if messages[i].get("role") == "user" and isinstance(messages[i].get("content"), str):
            um_seen += 1
            if um_seen >= observed_count:
                return i + 1
    return 0


# ═══════════════════════════════════════════════════════════════════
# 4. OBSERVATION STORE — batch files + index.json
# ═══════════════════════════════════════════════════════════════════

def _obs_dir(config: Config) -> str:
    return os.path.join(config.session_dir, "observations")

def load_index(config: Config) -> ObservationIndex:
    path = os.path.join(_obs_dir(config), "index.json")
    if not os.path.exists(path):
        return ObservationIndex()
    with open(path) as f:
        data = json.load(f)
    return ObservationIndex(
        last_observed_user_msg=data.get("last_observed_user_msg", 0),
        batches=data.get("batches", []),
    )

def save_index(config: Config, index: ObservationIndex):
    d = _obs_dir(config)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "index.json")
    with open(path, "w") as f:
        json.dump({
            "last_observed_user_msg": index.last_observed_user_msg,
            "batches": index.batches,
        }, f, indent=2)

def save_batch(config: Config, index: ObservationIndex, text: str,
               user_start: int, user_end: int) -> ObservationIndex:
    """Write a new batch file, update and save the index."""
    d = _obs_dir(config)
    os.makedirs(d, exist_ok=True)
    batch_id = len(index.batches) + 1
    filename = f"obs-{batch_id:04d}-u{user_start:04d}-u{user_end:04d}.md"
    with open(os.path.join(d, filename), "w") as f:
        f.write(text)
    index.batches.append({
        "id": batch_id,
        "file": filename,
        "user_range": [user_start, user_end],
        "created_at": datetime.now().isoformat(),
        "model": config.observer_model,
    })
    index.last_observed_user_msg = user_end
    save_index(config, index)
    return index

def load_all_observations(config: Config, index: ObservationIndex) -> str:
    """Concatenate all batch files into one string."""
    d = _obs_dir(config)
    parts = []
    for batch in index.batches:
        path = os.path.join(d, batch["file"])
        if os.path.exists(path):
            with open(path) as f:
                parts.append(f.read().strip())
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# 5. DECIDE — pure function, no I/O
# ═══════════════════════════════════════════════════════════════════

def build_plan(ts: TranscriptSlice, index: ObservationIndex, config: Config) -> Plan:
    if ts.keep_from == 0:
        return Plan(Mode.PASSTHROUGH, reason="nothing old to compress")

    observer_enabled = bool(config.observer_key and config.session_dir)
    if not observer_enabled or ts.old_user_count < config.observer_threshold:
        return Plan(Mode.TRIM_ALL, reason="observer not enabled or below threshold")

    delta = ts.old_user_count - index.last_observed_user_msg
    pending_start = find_pending_start(ts.all_messages, ts.keep_from, index.last_observed_user_msg)

    if delta <= 0 and index.batches:
        return Plan(Mode.CACHED, pending_start=pending_start,
                    reason=f"no new messages ({ts.old_user_count} observed)")

    if delta < config.observer_batch and index.batches:
        return Plan(Mode.TRIM_PENDING, pending_start=pending_start,
                    reason=f"delta {delta}/{config.observer_batch}, trimming pending")

    return Plan(Mode.OBSERVE, pending_start=pending_start,
                reason=f"observing {delta} new user messages")


# ═══════════════════════════════════════════════════════════════════
# 6. EXECUTE — side effects (API calls, file writes)
# ═══════════════════════════════════════════════════════════════════

def summarize_tool(tool_use_id: str, result_content: str, tool_use_map: dict) -> str:
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

def trim_tool_results(messages: list, tool_use_map: dict) -> tuple[list, int]:
    """Replace large tool results with summaries. Returns (messages, trim_count)."""
    trimmed = 0
    for msg in messages:
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
                    msg["content"][j]["content"] = summarize_tool(tid, original, tool_use_map)
                    trimmed += 1
    return messages, trimmed

def build_transcript(messages: list, tool_use_map: dict) -> str:
    """Format messages into a text transcript for the observer."""
    lines = []
    for msg in messages:
        role = msg.get("role", "?").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            if not content.strip():
                continue
            lines.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type", "")
                if btype == "text" and block.get("text", "").strip():
                    lines.append(f"[{role}]: {block['text']}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp_str = json.dumps(block.get("input", {}))
                    if len(inp_str) > 300:
                        inp_str = inp_str[:300] + "..."
                    lines.append(f"[{role} tool_call]: {name}({inp_str})")
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    tc = block.get("content", "")
                    if len(tc) > 2000:
                        tc = tc[:2000] + f"\n... [{len(tc)} chars total, truncated]"
                    info = tool_use_map.get(tid, {"name": "?", "input": {}})
                    lines.append(f"[TOOL_RESULT ({info['name']})]: {tc}")
    return "\n".join(lines)

def load_prompt(name: str) -> str:
    """Load prompt template from file next to this script."""
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path) as f:
        return f.read()

def call_observer(existing_obs: str, transcript: str, config: Config) -> str:
    """Call the observer LLM. Returns observation text."""
    if existing_obs:
        template = load_prompt("15-context-observer-append.md")
        prompt = template.replace("{{existing}}", existing_obs).replace("{{transcript}}", transcript)
    else:
        template = load_prompt("15-context-observer.md")
        prompt = template.replace("{{date}}", datetime.now().strftime("%Y-%m-%d")).replace("{{transcript}}", transcript)

    api_url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{config.observer_model}:generateContent?key={config.observer_key}")
    api_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 4096, "thinkingConfig": {"thinkingBudget": 0}},
    })

    result = subprocess.run(
        ["curl", "-s", api_url, "-H", "Content-Type: application/json", "-d", api_body],
        capture_output=True, text=True, timeout=60,
    )
    resp = json.loads(result.stdout)
    parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))

    if not text.strip():
        raise ValueError("Empty observation from API")

    # Strip markdown code fences
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])

    usage = resp.get("usageMetadata", {})
    in_tok = usage.get("promptTokenCount", "?")
    out_tok = usage.get("candidatesTokenCount", "?")
    log(f"observer call: {in_tok} in, {out_tok} out tokens")

    return text.strip()

def execute_plan(plan: Plan, ts: TranscriptSlice, index: ObservationIndex,
                 config: Config) -> ExecutionResult:
    """Execute the plan. Only place with side effects."""
    messages = ts.all_messages
    keep_from = ts.keep_from

    if plan.mode == Mode.PASSTHROUGH:
        return ExecutionResult()

    if plan.mode == Mode.TRIM_ALL:
        old = list(messages[:keep_from])  # shallow copy
        _, count = trim_tool_results(old, ts.tool_use_map)
        if count:
            log(f"phase 1: trimmed {count} old tool results")
        return ExecutionResult(pending_messages=old)

    if plan.mode == Mode.CACHED:
        obs_text = load_all_observations(config, index)
        return ExecutionResult(observation_text=obs_text)

    if plan.mode == Mode.TRIM_PENDING:
        obs_text = load_all_observations(config, index)
        pending = list(messages[plan.pending_start:keep_from])
        _, count = trim_tool_results(pending, ts.tool_use_map)
        if count:
            log(f"phase 1: trimmed {count} pending tool results")
        return ExecutionResult(observation_text=obs_text, pending_messages=pending)

    if plan.mode == Mode.OBSERVE:
        obs_text = load_all_observations(config, index)
        delta_msgs = messages[plan.pending_start:keep_from]
        new_user_count = count_user_messages(delta_msgs)

        try:
            transcript = build_transcript(delta_msgs, ts.tool_use_map)
            new_obs = call_observer(obs_text, transcript, config)

            user_start = index.last_observed_user_msg + 1
            user_end = ts.old_user_count
            index = save_batch(config, index, new_obs, user_start, user_end)

            label = f"+{new_user_count} new" if obs_text else f"{ts.old_user_count} total"
            log(f"phase 2: observed {label} user messages → {len(new_obs)} chars")

            return ExecutionResult(observation_text=load_all_observations(config, index))

        except Exception as e:
            log(f"phase 2 failed ({e}), falling back to phase 1")
            old = list(messages[:keep_from])
            _, count = trim_tool_results(old, ts.tool_use_map)
            if count:
                log(f"phase 1 fallback: trimmed {count} tool results")
            return ExecutionResult(pending_messages=old, warnings=[str(e)])

    raise ValueError(f"Unknown mode: {plan.mode}")


# ═══════════════════════════════════════════════════════════════════
# 7. ASSEMBLE — build output, single path
# ═══════════════════════════════════════════════════════════════════

def assemble(ts: TranscriptSlice, result: ExecutionResult, plan: Plan) -> list:
    """Build the final message list. Only place that constructs output messages."""
    messages = []

    # Observation summary (if we have one)
    if result.observation_text:
        messages.append({
            "role": "user",
            "content": f"[Session history — compressed observation of earlier messages]\n\n{result.observation_text}",
        })
        messages.append({
            "role": "assistant",
            "content": "Understood. I have the compressed session history. Continuing from where we left off.",
        })

    # Pending messages (trimmed delta between observation and recent window)
    messages.extend(result.pending_messages)

    # Recent messages (verbatim, untouched)
    recent = ts.all_messages[ts.keep_from:]
    messages.extend(recent)

    return messages


# ═══════════════════════════════════════════════════════════════════
# 8. NORMALIZE — enforce API transport contract
# ═══════════════════════════════════════════════════════════════════

def normalize_messages_for_api(msgs: list) -> list:
    """Ensure strict user/assistant alternation for Anthropic API.
    Merges consecutive same-role messages, inserts synthetic fillers."""
    if not msgs:
        return msgs
    fixed = [msgs[0]]
    for m in msgs[1:]:
        if m["role"] != fixed[-1]["role"]:
            fixed.append(m)
            continue
        prev_c = fixed[-1].get("content", "")
        curr_c = m.get("content", "")
        if m["role"] == "user":
            if isinstance(prev_c, str) and isinstance(curr_c, str):
                fixed[-1]["content"] = prev_c + "\n\n" + curr_c
            elif isinstance(prev_c, list) and isinstance(curr_c, list):
                fixed[-1]["content"] = prev_c + curr_c
            else:
                fixed.append({"role": "assistant", "content": "I'll continue."})
                fixed.append(m)
        else:  # assistant
            if isinstance(prev_c, str) and isinstance(curr_c, str):
                fixed[-1]["content"] = prev_c + "\n\n" + curr_c
            elif isinstance(prev_c, list) and isinstance(curr_c, list):
                fixed[-1]["content"] = prev_c + curr_c
            elif isinstance(prev_c, str) and isinstance(curr_c, list):
                fixed[-1] = {"role": "assistant", "content": [{"type": "text", "text": prev_c}] + curr_c}
            elif isinstance(prev_c, list) and isinstance(curr_c, str):
                fixed[-1]["content"] = prev_c + [{"type": "text", "text": curr_c}]
            else:
                fixed.append(m)
    return fixed


# ═══════════════════════════════════════════════════════════════════
# 9. LOGGING
# ═══════════════════════════════════════════════════════════════════

def log(msg: str):
    print(f"[context] {msg}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════
# 10. MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    raw_payload = json.loads(sys.stdin.read())
    messages = raw_payload.get("messages", [])
    config = load_config()

    try:
        ts = analyze_transcript(messages, config)
        index = load_index(config) if config.session_dir else ObservationIndex()
        plan = build_plan(ts, index, config)
        log(f"{plan.mode.value}: {plan.reason}")
        result = execute_plan(plan, ts, index, config)
        final_messages = assemble(ts, result, plan)
        final_messages = normalize_messages_for_api(final_messages)
    except Exception as e:
        log(f"error ({e}), passing through unchanged")
        final_messages = normalize_messages_for_api(messages)

    raw_payload["messages"] = final_messages
    print(json.dumps(raw_payload))


if __name__ == "__main__":
    main()
