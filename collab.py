#!/usr/bin/env python3
"""
Claude Code Collaboration Harness
==================================
Real-time collaboration between multiple Claude Code instances.
Zero external dependencies — Python 3.12+ stdlib only.

Architecture:
  - Pure file-based state (JSON) in a shared `state/` directory
  - OS-level file locking for concurrent access safety
  - Each Claude Code instance is a "node" identified by a unique name
  - Nodes communicate via messages, share context, coordinate tasks, and lock files
  - The `poll` command gives each node a real-time feed of changes

Usage:
    python collab.py <command> [args...]
    python collab.py --help
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "2.0.0"

# ── Configuration ─────────────────────────────────────────────

SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent

DEFAULT_STATE_DIR = Path(os.environ.get(
    "COLLAB_STATE_DIR",
    str(SCRIPT_DIR / "state")
))
LOCK_TIMEOUT = 5          # seconds to wait for OS file lock
STALE_LOCK_SEC = 10       # seconds before a lock file is considered stale
LOG_MAX = 1000            # max activity log entries
MSG_MAX = 500             # max messages to retain
FILE_LOCK_EXPIRY = 1800   # 30 minutes — auto-expire file locks older than this
STALE_NODE_SEC = 300      # 5 minutes — mark node as stale if no heartbeat

# Per-role identity — ANSI colors and Windows Terminal tab colors
ROLE_STYLES = {
    "lead": {"ansi": "\033[1;33m", "label": "LEAD", "tab_hex": "#E5A00D",
             "desc": "Coordination & Architecture"},
    "dev1": {"ansi": "\033[1;36m", "label": "DEV 1", "tab_hex": "#00B4D8",
             "desc": "Primary Implementation"},
    "dev2": {"ansi": "\033[1;32m", "label": "DEV 2", "tab_hex": "#2DC653",
             "desc": "Review, Testing & Secondary Dev"},
}
ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"


# ── Utilities ─────────────────────────────────────────────────

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)

def ago(iso: str) -> str:
    try:
        s = int((datetime.now(timezone.utc) - parse_ts(iso)).total_seconds())
        if s < 0: return "just now"
        if s < 60: return f"{s}s ago"
        if s < 3600: return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return "?"

def short_time(iso: str) -> str:
    try:
        return parse_ts(iso).strftime("%H:%M:%S")
    except Exception:
        return "??:??:??"

def trunc(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[:n - 3] + "..."


# ── Output Modes ────────────────────────────────────────────────

_json_mode = False
_brief_mode = False

def _emit_json(data: dict):
    """Print structured JSON output and exit. Used in --json mode."""
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


# ── Signal Files (push-style notification) ───────────���────────

def _signal_path(state_dir: Path, node: str) -> Path:
    return state_dir / f"_signal_{node}"

def signal_node(state_dir: Path, node: str, reason: str):
    """Touch a signal file for a node so it knows to poll.
    The file contains the reason, appended line by line."""
    p = _signal_path(state_dir, node)
    try:
        with open(str(p), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {reason}\n")
    except OSError:
        pass

def read_and_clear_signal(state_dir: Path, node: str) -> list:
    """Read all pending signal lines and clear the file. Returns list of strings."""
    p = _signal_path(state_dir, node)
    lines = []
    try:
        if p.exists():
            lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            p.unlink()
    except OSError:
        pass
    return lines


# ── Cross-Platform Window Control (via inject.py) ─────────────
# Supports: Windows (Win32 API), Linux/macOS (tmux), Linux/macOS (screen)

try:
    from inject import get_backend as _get_injection_backend, list_all_sessions
    _injection_backend = _get_injection_backend()
except ImportError:
    _injection_backend = None
    def list_all_sessions():
        return {}

def find_collab_window(node_name: str) -> str:
    """Find the session/PID for a collaboration node. Returns session ID or empty string."""
    if _injection_backend is None:
        return ""
    return _injection_backend.find_target(node_name) or ""

def _run_inject(target_node: str, text: str) -> bool:
    """Inject text into a target node's terminal."""
    if _injection_backend is None:
        print("[ERROR] No injection backend available (install tmux/screen on Linux/macOS)")
        return False
    return _injection_backend.inject(target_node, text)

def _run_interrupt(target_node: str) -> bool:
    """Send Escape to a target node's terminal."""
    if _injection_backend is None:
        print("[ERROR] No injection backend available (install tmux/screen on Linux/macOS)")
        return False
    return _injection_backend.interrupt(target_node)


# ── File Locking ──────────────────────────────────────────────

class FileLock:
    """Cross-process file lock via atomic exclusive-create."""

    def __init__(self, target: Path):
        self.lockpath = target.parent / (target.name + ".lock")

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        while time.time() < deadline:
            try:
                fd = os.open(str(self.lockpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except (FileExistsError, OSError):
                try:
                    if time.time() - os.path.getmtime(str(self.lockpath)) > STALE_LOCK_SEC:
                        os.unlink(str(self.lockpath))
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        raise TimeoutError(f"Lock timeout: {self.lockpath}")

    def __exit__(self, *_):
        try:
            os.unlink(str(self.lockpath))
        except OSError:
            pass


# ── State Manager ─────────────────────────────────────────────

_DEFAULTS = {
    "nodes": {},
    "messages": [],
    "context": {},
    "tasks": {},
    "locks": {},
    "log": [],
    "meta": {"next_task_id": 1},
}

class State:
    """JSON-file state store with OS-level locking for safe concurrent access."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        for name, default in _DEFAULTS.items():
            p = self._path(name)
            if not p.exists():
                self._write_raw(p, default)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    @staticmethod
    def _read_raw(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            name = path.stem
            default = _DEFAULTS.get(name, {})
            return list(default) if isinstance(default, list) else dict(default)

    @staticmethod
    def _write_raw(path: Path, data):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        if sys.platform == "win32" and path.exists():
            path.unlink()
        tmp.rename(path)

    def read(self, collection: str):
        return self._read_raw(self._path(collection))

    def write(self, collection: str, data):
        self._write_raw(self._path(collection), data)

    def update(self, collection: str, fn):
        """Lock -> read -> fn(data) -> write.  Returns fn's return value."""
        path = self._path(collection)
        with FileLock(path):
            data = self._read_raw(path)
            result = fn(data)
            self._write_raw(path, data)
            return result

    def append_log(self, actor: str, action: str, summary: str):
        def _do(log):
            log.append({"actor": actor, "action": action, "summary": summary, "at": utcnow()})
            while len(log) > LOG_MAX:
                log.pop(0)
        self.update("log", _do)

    def next_task_id(self) -> int:
        def _do(meta):
            tid = meta.get("next_task_id", 1)
            meta["next_task_id"] = tid + 1
            return tid
        return self.update("meta", _do)


# ══════════════════════════════════════════════════════════════
#  IDENTITY / BANNER
# ══════════════════════════════════════════════════════════════

def _print_banner(name: str, role: str = ""):
    """Print a role banner to visually identify the terminal."""
    style = ROLE_STYLES.get(name, {})
    color = style.get("ansi", "\033[1;37m")
    label = style.get("label", name.upper())
    desc = style.get("desc", role)
    tab_hex = style.get("tab_hex", "")

    # Set Windows Terminal tab color (ignored by other terminals)
    if tab_hex:
        sys.stdout.write(f"\033]9;4;3;{tab_hex}\033\\")
        sys.stdout.flush()

    # Set window title
    sys.stdout.write(f"\033]0;Collab: {label}\007")
    sys.stdout.flush()

    if _brief_mode:
        print(f"{color}[{label}]{ANSI_RESET} {desc}")
        return

    w = 48
    bar = "=" * w
    print(f"\n{color}+{bar}+")
    print(f"|{'':^{w}}|")
    print(f"|{f'***  {label}  ***':^{w}}|")
    print(f"|{desc:^{w}}|")
    print(f"|{'':^{w}}|")
    print(f"+{bar}+{ANSI_RESET}\n")


def cmd_whoami(state: State, name: str):
    """Print the role banner for this instance."""
    nodes = state.read("nodes") or {}
    node = nodes.get(name)
    role = node["role"] if node else ""
    _print_banner(name, role)
    if node:
        print(f"  Node:   {name}")
        print(f"  Role:   {node['role']}")
        print(f"  Status: {node['status']}")
        print(f"  Joined: {node.get('joined_at', 'unknown')}")
    else:
        print(f"  Node \"{name}\" is not registered. Run: collab join {name}")


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

def _touch_heartbeat(state: State, name: str):
    """Silently update a node's heartbeat timestamp. Called automatically by
    every command that identifies the acting node, so separate heartbeat
    calls are unnecessary — reduces token overhead."""
    def _do(nodes):
        if name in nodes:
            nodes[name]["last_heartbeat"] = utcnow()
    state.update("nodes", _do)


# ── Nodes ─────────────────────────────────────────────────────

def cmd_join(state: State, name: str, role: str = "general"):
    def _do(nodes):
        existing = name in nodes
        nodes[name] = {
            "name": name,
            "role": role,
            "status": "active",
            "working_on": "",
            "joined_at": nodes[name]["joined_at"] if existing else utcnow(),
            "last_heartbeat": utcnow(),
            "last_poll": utcnow(),
        }
        return existing
    was_existing = state.update("nodes", _do)
    verb = "Rejoined" if was_existing else "Joined"
    state.append_log(name, "joined", f'{name} joined as "{role}"')
    if _json_mode:
        return _emit_json({"command": "join", "ok": True, "name": name, "role": role,
                           "rejoined": was_existing})
    _print_banner(name, role)
    print(f'[OK] {verb} as "{name}" (role: {role})')


def cmd_leave(state: State, name: str):
    def _do(nodes):
        return nodes.pop(name, None) is not None
    found = state.update("nodes", _do)
    if not found:
        print(f'[WARN] "{name}" was not registered')
        return
    # Release any file locks held by this node
    def _release(locks):
        released = [f for f, v in locks.items() if v["held_by"] == name]
        for f in released:
            del locks[f]
        return released
    released = state.update("locks", _release)
    state.append_log(name, "left", f"{name} left the collaboration")
    print(f'[OK] "{name}" has left')
    if released:
        print(f"     Released {len(released)} file lock(s)")


def cmd_status(state: State, compact: bool = False):
    nodes = state.read("nodes")
    tasks = state.read("tasks")
    ctx   = state.read("context")
    locks = state.read("locks")
    log_entries = state.read("log")

    if _json_mode:
        return _emit_json({
            "command": "status",
            "nodes": nodes,
            "tasks": tasks,
            "context": ctx,
            "locks": locks,
            "recent_log": log_entries[-8:],
        })

    n_open = sum(1 for t in tasks.values() if t["status"] == "open")
    n_active = sum(1 for t in tasks.values() if t["status"] == "active")
    n_done = sum(1 for t in tasks.values() if t["status"] == "done")

    if compact:
        # ── Compact mode: dense single-line-per-item output ──
        print(f"[status] {len(nodes)} nodes | {len(tasks)} tasks ({n_active} active, {n_done} done, {n_open} open) | {len(locks)} locks")
        if nodes:
            node_parts = []
            for n in sorted(nodes.values(), key=lambda x: x.get("joined_at", "")):
                hb = ago(n.get("last_heartbeat", ""))
                node_parts.append(f'{n["name"]}[{n.get("status", "?")[0]}]({hb})')
            print(f"  nodes: {', '.join(node_parts)}")
        active_tasks = [t for t in tasks.values() if t["status"] in ("active", "claimed", "blocked")]
        if active_tasks:
            task_parts = []
            for t in sorted(active_tasks, key=lambda x: x["id"]):
                icons = {"claimed": "*", "active": ">", "blocked": "x"}
                icon = icons.get(t["status"], "?")
                who = t.get("assigned_to", "?") or "?"
                task_parts.append(f'#{t["id"]}[{icon}]{who}:{trunc(t["title"], 25)}')
            print(f"  tasks: {', '.join(task_parts)}")
        if locks:
            lock_parts = [f'{fp}->{info["held_by"]}' for fp, info in locks.items()]
            print(f"  locks: {', '.join(lock_parts)}")
        return

    # ── Full mode ──
    print("=== Collaboration Status ===")
    print(f"    {len(nodes)} node(s) | {len(tasks)} task(s) ({n_open} open)"
          f" | {len(ctx)} context entries | {len(locks)} lock(s)\n")

    # Nodes
    print(f"Nodes ({len(nodes)}):")
    if not nodes:
        print('  (none -- run: collab join <name> --role "<role>")')
    for n in sorted(nodes.values(), key=lambda x: x.get("joined_at", "")):
        s = n.get("status", "?")
        w = trunc(n.get("working_on", "") or "-", 30)
        hb = ago(n.get("last_heartbeat", ""))
        print(f'  * {n["name"]:<15} [{s:<6}]  {w:<32} ({hb})')

    # Tasks
    print(f"\nTasks ({len(tasks)}):")
    if not tasks:
        print("  (none)")
    for t in sorted(tasks.values(), key=lambda x: x["id"]):
        icons = {"open": "o", "claimed": "*", "active": ">", "done": "v", "blocked": "x"}
        icon = icons.get(t["status"], "?")
        who = f'({t["assigned_to"]})' if t.get("assigned_to") else ""
        pri = t.get("priority", "medium")
        tag = f"  {pri.upper()}" if pri != "medium" else ""
        print(f'  #{t["id"]:<3} [{icon}] {t["status"]:<7}  {trunc(t["title"], 38):<40} {who:<15}{tag}')

    # Context
    print(f"\nShared Context ({len(ctx)}):")
    if not ctx:
        print("  (none)")
    entries = list(ctx.items())
    for k, v in entries[:15]:
        val = trunc(str(v["value"]), 45)
        by = v.get("set_by", "?")
        when = ago(v.get("set_at", ""))
        print(f"  {k} = {val}  (by {by}, {when})")
    if len(entries) > 15:
        print(f"  ... and {len(entries) - 15} more")

    # Locks
    print(f"\nFile Locks ({len(locks)}):")
    if not locks:
        print("  (none)")
    for fp, info in locks.items():
        print(f"  {fp}  ->  {info['held_by']}  ({ago(info.get('acquired_at', ''))})")

    # Recent log
    recent = log_entries[-8:]
    print("\nRecent Activity:")
    if not recent:
        print("  (none)")
    for entry in recent:
        print(f"  [{short_time(entry['at'])}] {entry['summary']}")


def cmd_heartbeat(state: State, name: str, working_on=None, node_status=None):
    def _do(nodes):
        if name not in nodes:
            return False
        nodes[name]["last_heartbeat"] = utcnow()
        if working_on is not None:
            nodes[name]["working_on"] = working_on
        if node_status is not None:
            nodes[name]["status"] = node_status
        return True
    found = state.update("nodes", _do)
    if not found:
        print(f'[ERROR] Node "{name}" not found')
        print(f'  Fix: run `collab join {name} --role "<your-role>"`')
        sys.exit(1)
    parts = []
    if working_on is not None:
        parts.append(f'working on: "{trunc(working_on, 40)}"')
    if node_status is not None:
        parts.append(f"status: {node_status}")
    detail = " | ".join(parts) if parts else "heartbeat"
    print(f"[OK] {name}: {detail}")


# ── Messages ──────────────────────────────────────────────────

def cmd_send(state: State, from_node: str, to_node: str, message: str):
    _touch_heartbeat(state, from_node)
    nodes = state.read("nodes")
    if to_node not in nodes:
        active = ", ".join(sorted(nodes.keys())) or "(none)"
        print(f'[ERROR] Node "{to_node}" not found')
        print(f'  Active nodes: {active}')
        sys.exit(1)
    msg = {
        "from": from_node, "to": to_node,
        "content": message, "at": utcnow(), "type": "direct",
    }
    def _do(messages):
        messages.append(msg)
        while len(messages) > MSG_MAX:
            messages.pop(0)
    state.update("messages", _do)
    signal_node(state.dir, to_node, f"Message from {from_node}")
    state.append_log(from_node, "sent", f'{from_node} -> {to_node}: "{trunc(message, 50)}"')
    print(f'[OK] Message sent to "{to_node}"')


def cmd_broadcast(state: State, from_node: str, message: str):
    _touch_heartbeat(state, from_node)
    nodes = state.read("nodes")
    others = [n for n in nodes if n != from_node]
    msg = {
        "from": from_node, "to": "all",
        "content": message, "at": utcnow(), "type": "broadcast",
    }
    def _do(messages):
        messages.append(msg)
        while len(messages) > MSG_MAX:
            messages.pop(0)
    state.update("messages", _do)
    for other in others:
        signal_node(state.dir, other, f"Broadcast from {from_node}")
    state.append_log(from_node, "broadcast", f'{from_node} -> all: "{trunc(message, 50)}"')
    print(f"[OK] Broadcast sent ({len(others)} other node(s))")


def cmd_inbox(state: State, name: str, show_all: bool = False, limit: int = 20):
    _touch_heartbeat(state, name)
    messages = state.read("messages")
    nodes = state.read("nodes")
    last_poll = nodes.get(name, {}).get("last_poll", "1970-01-01T00:00:00+00:00")

    relevant = [
        m for m in messages
        if m["to"] == name or (m["to"] == "all" and m["from"] != name)
    ]
    if not show_all:
        relevant = [m for m in relevant if m["at"] > last_poll]
    relevant = relevant[-limit:]

    if _json_mode:
        return _emit_json({"command": "inbox", "node": name, "messages": relevant})

    if not relevant:
        print("No new messages." if not show_all else "No messages.")
        return

    label = "All messages" if show_all else "New messages"
    print(f'{label} for "{name}" ({len(relevant)}):\n')
    for m in relevant:
        t = short_time(m["at"])
        src = m["from"]
        tag = "broadcast" if m["to"] == "all" else "-> you"
        print(f"  [{t}] {src} ({tag}): {m['content']}")


# ── Context ───────────────────────────────────────────────────

def cmd_context_set(state: State, key: str, value: str, by: str = "system"):
    if by != "system":
        _touch_heartbeat(state, by)
    def _do(ctx):
        ctx[key] = {"value": value, "set_by": by, "set_at": utcnow()}
    state.update("context", _do)
    state.append_log(by, "context_set", f'{by} set context "{key}"')
    print(f'[OK] Context "{key}" set by {by}')


def cmd_context_get(state: State, key=None):
    ctx = state.read("context")
    if _json_mode:
        if key and key not in ctx:
            return _emit_json({"command": "context_get", "error": f"Key \"{key}\" not found"})
        data = {key: ctx[key]} if key else ctx
        return _emit_json({"command": "context_get", "context": data})
    if key:
        if key not in ctx:
            available = ", ".join(sorted(ctx.keys())[:10]) or "(empty)"
            print(f'[ERROR] Context key "{key}" not found')
            print(f'  Available keys: {available}')
            sys.exit(1)
        e = ctx[key]
        print(f"Key:   {key}")
        print(f"Value: {e['value']}")
        print(f"Set:   {e.get('set_by', '?')} ({ago(e.get('set_at', ''))})")
    else:
        if not ctx:
            print("No shared context.")
            return
        print(f"Shared Context ({len(ctx)}):\n")
        for k, v in ctx.items():
            print(f"  {k} = {trunc(str(v['value']), 50)}")
            print(f"    (by {v.get('set_by', '?')}, {ago(v.get('set_at', ''))})")


def cmd_context_del(state: State, key: str):
    def _do(ctx):
        return ctx.pop(key, None) is not None
    found = state.update("context", _do)
    if not found:
        print(f'[ERROR] Context key "{key}" not found')
        print(f'  Use `context get` to list all keys')
        sys.exit(1)
    state.append_log("system", "context_del", f'Deleted context "{key}"')
    print(f'[OK] Context "{key}" deleted')


def cmd_context_append(state: State, key: str, value: str, by: str = "system"):
    if by != "system":
        _touch_heartbeat(state, by)
    def _do(ctx):
        if key in ctx:
            old = ctx[key]["value"]
            ctx[key] = {"value": old + "\n" + value, "set_by": by, "set_at": utcnow()}
        else:
            ctx[key] = {"value": value, "set_by": by, "set_at": utcnow()}
    state.update("context", _do)
    state.append_log(by, "context_append", f'{by} appended to context "{key}"')
    print(f'[OK] Appended to context "{key}"')


# ── Tasks ─────────────────────────────────────────────────────

def cmd_task_add(state: State, title: str, desc: str = "", assign: str = "",
                 priority: str = "medium", by: str = "system",
                 depends_on: str = ""):
    if by != "system":
        _touch_heartbeat(state, by)
    tid = state.next_task_id()
    deps = [int(d.strip()) for d in depends_on.split(",") if d.strip()] if depends_on else []
    task = {
        "id": tid, "title": title, "description": desc,
        "status": "claimed" if assign else "open",
        "priority": priority, "created_by": by,
        "assigned_to": assign or None,
        "depends_on": deps,
        "comments": [],
        "created_at": utcnow(), "updated_at": utcnow(),
        "result": "",
        "history": [{"action": "created", "by": by, "at": utcnow()}],
    }
    if assign:
        task["history"].append({"action": f"assigned to {assign}", "by": by, "at": utcnow()})
    if deps:
        task["history"].append({"action": f"depends on #{', #'.join(str(d) for d in deps)}", "by": by, "at": utcnow()})
    def _do(tasks):
        tasks[str(tid)] = task
    state.update("tasks", _do)

    summary = f'Task #{tid} created: "{trunc(title, 40)}"'
    if assign:
        summary += f" (assigned to {assign})"
        signal_node(state.dir, assign, f"Task #{tid} assigned to you by {by}")
    if deps:
        summary += f" (depends on #{', #'.join(str(d) for d in deps)})"
    state.append_log(by, "task_created", summary)
    print(f"[OK] {summary}")


_PRI_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_STATUS_ORDER = {"active": 0, "blocked": 1, "claimed": 2, "open": 3, "done": 4}

def cmd_task_list(state: State, status_filter=None, assigned_filter=None):
    tasks = state.read("tasks")
    items = sorted(tasks.values(), key=lambda t: (
        _STATUS_ORDER.get(t["status"], 9),
        _PRI_ORDER.get(t.get("priority", "medium"), 9),
        t["id"],
    ))
    if status_filter:
        items = [t for t in items if t["status"] == status_filter]
    if assigned_filter:
        items = [t for t in items if t.get("assigned_to") == assigned_filter]

    if _json_mode:
        return _emit_json({"command": "task_list", "tasks": items})

    if not items:
        print("No tasks found.")
        return

    print(f"Tasks ({len(items)}):\n")
    for t in items:
        icons = {"open": "o", "claimed": "*", "active": ">", "done": "v", "blocked": "x"}
        icon = icons.get(t["status"], "?")
        who = f'({t["assigned_to"]})' if t.get("assigned_to") else ""
        pri = t.get("priority", "medium")
        tag = f"  {pri.upper()}" if pri != "medium" else ""
        deps = t.get("depends_on", [])
        dep_tag = f"  [needs #{',#'.join(str(d) for d in deps)}]" if deps else ""
        print(f'  #{t["id"]:<3} [{icon}] {t["status"]:<7}  {trunc(t["title"], 38):<40} {who:<15}{tag}{dep_tag}')


def cmd_task_claim(state: State, name: str, task_id: int):
    _touch_heartbeat(state, name)
    def _do(tasks):
        k = str(task_id)
        if k not in tasks:
            return "not_found"
        t = tasks[k]
        if t["status"] == "done":
            return "already_done"
        t["status"] = "claimed"
        t["assigned_to"] = name
        t["updated_at"] = utcnow()
        t["history"].append({"action": f"claimed by {name}", "by": name, "at": utcnow()})
        return "ok"
    result = state.update("tasks", _do)
    if result == "not_found":
        print(f"[ERROR] Task #{task_id} not found")
        print(f'  Use `task list` to see available tasks')
        sys.exit(1)
    if result == "already_done":
        print(f"[ERROR] Task #{task_id} is already done — cannot claim")
        print(f'  Use `task show {task_id}` to see its result')
        sys.exit(1)
    state.append_log(name, "task_claimed", f"{name} claimed task #{task_id}")
    print(f'[OK] "{name}" claimed task #{task_id}')


def cmd_task_update(state: State, task_id: int, new_status: str,
                    result_text: str = "", by: str = "system"):
    if by != "system":
        _touch_heartbeat(state, by)
    def _do(tasks):
        k = str(task_id)
        if k not in tasks:
            return "not_found"
        t = tasks[k]
        old = t["status"]
        t["status"] = new_status
        t["updated_at"] = utcnow()
        if result_text:
            t["result"] = result_text
        t["history"].append({"action": f"{old} -> {new_status}", "by": by, "at": utcnow()})
        return "ok"
    result = state.update("tasks", _do)
    if result == "not_found":
        print(f"[ERROR] Task #{task_id} not found")
        print(f'  Use `task list` to see available tasks')
        sys.exit(1)
    log_msg = f"Task #{task_id} -> {new_status}"
    if result_text:
        log_msg += f': "{trunc(result_text, 40)}"'
    state.append_log(by, "task_updated", log_msg)
    print(f"[OK] Task #{task_id} -> {new_status}")


def cmd_task_show(state: State, task_id: int):
    tasks = state.read("tasks")
    k = str(task_id)
    if k not in tasks:
        if _json_mode:
            return _emit_json({"command": "task_show", "error": f"Task #{task_id} not found"})
        print(f"[ERROR] Task #{task_id} not found")
        print(f'  Use `task list` to see available tasks')
        sys.exit(1)
    t = tasks[k]

    if _json_mode:
        return _emit_json({"command": "task_show", "task": t})

    print(f'Task #{t["id"]}: {t["title"]}')
    print(f'  Status:      {t["status"]}')
    print(f'  Priority:    {t.get("priority", "medium")}')
    print(f'  Assigned:    {t.get("assigned_to") or "(unassigned)"}')
    print(f'  Created by:  {t.get("created_by", "?")} ({ago(t.get("created_at", ""))})')
    deps = t.get("depends_on", [])
    if deps:
        dep_statuses = []
        for d in deps:
            dt = tasks.get(str(d))
            st = dt["status"] if dt else "?"
            dep_statuses.append(f"#{d}({st})")
        print(f'  Depends on:  {", ".join(dep_statuses)}')
    if t.get("description"):
        print(f'  Description: {t["description"]}')
    if t.get("result"):
        print(f'  Result:      {t["result"]}')
    comments = t.get("comments", [])
    if comments:
        print(f"  Comments ({len(comments)}):")
        for c in comments:
            print(f"    [{short_time(c['at'])}] {c['by']}: {c['text']}")
    print("  History:")
    for h in t.get("history", []):
        print(f"    [{short_time(h['at'])}] {h['action']} (by {h.get('by', '?')})")


def cmd_task_comment(state: State, task_id: int, text: str, by: str = "system"):
    if by != "system":
        _touch_heartbeat(state, by)
    def _do(tasks):
        k = str(task_id)
        if k not in tasks:
            return "not_found"
        t = tasks[k]
        if "comments" not in t:
            t["comments"] = []
        t["comments"].append({"text": text, "by": by, "at": utcnow()})
        t["updated_at"] = utcnow()
        return "ok"
    result = state.update("tasks", _do)
    if result == "not_found":
        print(f"[ERROR] Task #{task_id} not found")
        print(f'  Use `task list` to see available tasks')
        sys.exit(1)
    state.append_log(by, "task_comment", f'{by} commented on task #{task_id}: "{trunc(text, 40)}"')
    print(f"[OK] Comment added to task #{task_id}")


def cmd_task_reassign(state: State, task_id: int, new_assignee: str, by: str = "system"):
    if by != "system":
        _touch_heartbeat(state, by)
    def _do(tasks):
        k = str(task_id)
        if k not in tasks:
            return "not_found"
        t = tasks[k]
        old = t.get("assigned_to") or "(unassigned)"
        t["assigned_to"] = new_assignee
        if t["status"] == "active":
            t["status"] = "claimed"
        t["updated_at"] = utcnow()
        t["history"].append({"action": f"reassigned {old} -> {new_assignee}", "by": by, "at": utcnow()})
        return old
    result = state.update("tasks", _do)
    if result == "not_found":
        print(f"[ERROR] Task #{task_id} not found")
        print(f'  Use `task list` to see available tasks')
        sys.exit(1)
    signal_node(state.dir, new_assignee, f"Task #{task_id} reassigned to you by {by}")
    state.append_log(by, "task_reassigned", f"Task #{task_id} reassigned to {new_assignee}")
    print(f"[OK] Task #{task_id} reassigned to {new_assignee}")


# ── File Locks ────────────────────────────────────────────────

def _expire_stale_locks(locks: dict) -> list:
    """Remove locks older than FILE_LOCK_EXPIRY. Returns list of expired (file, holder)."""
    expired = []
    now = datetime.now(timezone.utc)
    for fp, info in list(locks.items()):
        try:
            lock_age = (now - parse_ts(info["acquired_at"])).total_seconds()
            if lock_age > FILE_LOCK_EXPIRY:
                expired.append((fp, info["held_by"]))
                del locks[fp]
        except Exception:
            pass
    return expired


def cmd_lock(state: State, name: str, filepath: str):
    _touch_heartbeat(state, name)
    def _do(locks):
        # Auto-expire stale locks
        _expire_stale_locks(locks)
        if filepath in locks:
            holder = locks[filepath]["held_by"]
            return "yours" if holder == name else f"held:{holder}"
        locks[filepath] = {"held_by": name, "acquired_at": utcnow()}
        return "ok"
    result = state.update("locks", _do)
    if result == "yours":
        print(f'[OK] Already locked by you: "{filepath}"')
        return
    if result.startswith("held:"):
        holder = result[5:]
        print(f'[ERROR] "{filepath}" is locked by "{holder}"')
        print(f'  Wait for them to finish, or ask: `send {name} {holder} "Please unlock {filepath}"`')
        sys.exit(1)
    state.append_log(name, "locked", f'{name} locked "{filepath}"')
    print(f'[OK] Locked "{filepath}"')


def cmd_unlock(state: State, name: str, filepath: str):
    _touch_heartbeat(state, name)
    def _do(locks):
        if filepath not in locks:
            return "not_locked"
        if locks[filepath]["held_by"] != name:
            return f"held:{locks[filepath]['held_by']}"
        del locks[filepath]
        return "ok"
    result = state.update("locks", _do)
    if result == "not_locked":
        print(f'[OK] "{filepath}" was not locked')
        return
    if result.startswith("held:"):
        holder = result[5:]
        print(f'[ERROR] Cannot unlock "{filepath}" — locked by "{holder}", not you')
        print(f'  Only "{holder}" can unlock it, or it will auto-expire after {FILE_LOCK_EXPIRY // 60}m')
        sys.exit(1)
    state.append_log(name, "unlocked", f'{name} unlocked "{filepath}"')
    print(f'[OK] Unlocked "{filepath}"')


def cmd_locks(state: State):
    locks = state.read("locks")
    if _json_mode:
        return _emit_json({"command": "locks", "locks": locks})
    if not locks:
        print("No active file locks.")
        return
    print(f"File Locks ({len(locks)}):\n")
    for fp, info in locks.items():
        print(f"  {fp}  ->  {info['held_by']}  ({ago(info.get('acquired_at', ''))})")


# ── Poll ──────────────────────────────────────────────────────

def _check_stale_nodes(nodes: dict) -> list:
    """Return list of (name, seconds_since_heartbeat) for stale nodes."""
    stale = []
    now = datetime.now(timezone.utc)
    for n, info in nodes.items():
        try:
            hb = info.get("last_heartbeat", info.get("joined_at", ""))
            if hb:
                age = (now - parse_ts(hb)).total_seconds()
                if age > STALE_NODE_SEC:
                    stale.append((n, int(age)))
        except Exception:
            pass
    return stale


def cmd_poll(state: State, name: str):
    nodes = state.read("nodes")
    if name not in nodes:
        if _json_mode:
            return _emit_json({"command": "poll", "error": f"Node \"{name}\" not found"})
        print(f'[ERROR] Node "{name}" not found')
        print(f'  Fix: run `collab join {name} --role "<your-role>"`')
        sys.exit(1)

    # Clear any pending signal file
    signals = read_and_clear_signal(state.dir, name)

    last_poll = nodes[name].get("last_poll", "1970-01-01T00:00:00+00:00")
    messages  = state.read("messages")
    log_data  = state.read("log")
    tasks     = state.read("tasks")
    locks     = state.read("locks")

    # New messages addressed to this node (or broadcast)
    new_msgs = [
        m for m in messages
        if m["at"] > last_poll
        and (m["to"] == name or (m["to"] == "all" and m["from"] != name))
    ]

    # Activity by OTHER nodes since last poll
    new_activity = [
        e for e in log_data
        if e["at"] > last_poll and e.get("actor") != name
    ]

    # Advance last_poll + heartbeat
    def _do(nodes_data):
        if name in nodes_data:
            nodes_data[name]["last_poll"] = utcnow()
            nodes_data[name]["last_heartbeat"] = utcnow()
    state.update("nodes", _do)

    if _json_mode:
        my_tasks = [t for t in tasks.values()
                    if t.get("assigned_to") == name and t["status"] not in ("done",)]
        return _emit_json({
            "command": "poll",
            "node": name,
            "new_messages": new_msgs,
            "new_activity": new_activity,
            "my_tasks": my_tasks,
            "signals": signals,
        })

    # Auto-expire stale locks
    def _clean_locks(lock_data):
        return _expire_stale_locks(lock_data)
    expired_locks = state.update("locks", _clean_locks)

    has_updates = bool(new_msgs or new_activity)

    # Brief mode limits
    msg_limit = 5 if _brief_mode else 50
    activity_limit = 10 if _brief_mode else 25

    if _brief_mode:
        print(f'--- poll {name} ---')
    else:
        print(f'=== Updates for "{name}" ===\n')

    # Warnings first
    stale = _check_stale_nodes(nodes)
    if stale:
        for sn, sec in stale:
            m, s = divmod(sec, 60)
            print(f"  [!] STALE: {sn} ({m}m{s}s)")

    if expired_locks:
        for fp, holder in expired_locks:
            print(f"  [!] LOCK EXPIRED: {fp} (was {holder})")

    if new_msgs:
        shown = new_msgs[-msg_limit:]
        if len(new_msgs) > msg_limit:
            print(f"Messages ({len(new_msgs)}, showing last {msg_limit}):")
        else:
            print(f"Messages ({len(new_msgs)}):")
        for m in shown:
            src = m["from"]
            tag = "bc" if m["to"] == "all" else "dm"
            content = trunc(m['content'], 80) if _brief_mode else m['content']
            print(f"  [{short_time(m['at'])}] {src}({tag}): {content}")
        if not _brief_mode:
            print()

    # Non-message activity from others
    other = [e for e in new_activity if e["action"] not in ("sent", "broadcast")]
    if other:
        shown = other[-activity_limit:]
        if len(other) > activity_limit:
            print(f"Activity ({len(other)}, showing last {activity_limit}):")
        else:
            print(f"Activity ({len(other)}):")
        for e in shown:
            print(f"  [{short_time(e['at'])}] {trunc(e['summary'], 70)}")
        if not _brief_mode:
            print()

    # Your tasks
    my_tasks = sorted(
        [t for t in tasks.values()
         if t.get("assigned_to") == name and t["status"] not in ("done",)],
        key=lambda t: (_PRI_ORDER.get(t.get("priority", "medium"), 9), t["id"]),
    )
    if my_tasks:
        print(f"Your Tasks ({len(my_tasks)}):")
        for t in my_tasks:
            icons = {"open": "o", "claimed": "*", "active": ">", "blocked": "x"}
            icon = icons.get(t["status"], "?")
            pri = t.get("priority", "medium")
            tag = f" {pri.upper()}" if pri != "medium" else ""
            deps = t.get("depends_on", [])
            blocked = False
            if deps:
                blocked = any(
                    tasks.get(str(d), {}).get("status") != "done" for d in deps
                )
            block_tag = " [BLOCKED]" if blocked else ""
            title_len = 35 if _brief_mode else 45
            print(f"  #{t['id']:<3} [{icon}] {t['status']:<7} {trunc(t['title'], title_len)}{tag}{block_tag}")

    # Quick summary line
    active_nodes = [n for n in nodes.values() if n.get("status") in ("active", "busy")]
    open_tasks = [t for t in tasks.values() if t["status"] == "open"]
    all_my = [t for t in tasks.values()
              if t.get("assigned_to") == name and t["status"] not in ("done",)]
    if not _brief_mode:
        print()
    print(f"Summary: {len(active_nodes)} active, "
          f"{len(open_tasks)} open, {len(all_my)} yours")

    if not has_updates and not stale and not expired_locks:
        print("  (no new activity)")


# ── Pending (lightweight notification check) ─────────────────

def cmd_pending(state: State, name: str):
    """Ultra-fast check: do I have anything waiting? Returns signal lines + counts."""
    _touch_heartbeat(state, name)
    signals = read_and_clear_signal(state.dir, name)
    nodes = state.read("nodes")
    if name not in nodes:
        if _json_mode:
            return _emit_json({"command": "pending", "error": f"Node \"{name}\" not found"})
        print(f'[ERROR] Node "{name}" not found')
        print(f'  Fix: run `collab join {name} --role "<your-role>"`')
        sys.exit(1)

    last_poll = nodes[name].get("last_poll", "1970-01-01T00:00:00+00:00")
    messages = state.read("messages")
    tasks = state.read("tasks")

    new_msgs = [
        m for m in messages
        if m["at"] > last_poll
        and (m["to"] == name or (m["to"] == "all" and m["from"] != name))
    ]
    my_pending = [
        t for t in tasks.values()
        if t.get("assigned_to") == name and t["status"] in ("open", "claimed")
    ]

    if _json_mode:
        return _emit_json({
            "command": "pending",
            "node": name,
            "signals": signals,
            "new_message_count": len(new_msgs),
            "pending_task_count": len(my_pending),
        })

    total = len(new_msgs) + len(my_pending)
    if signals:
        print(f"[!] {len(signals)} signal(s):")
        for s in signals[-5:]:
            print(f"  {s}")
    if new_msgs:
        print(f"[!] {len(new_msgs)} new message(s) — run `poll {name}` to read")
    if my_pending:
        print(f"[!] {len(my_pending)} task(s) waiting for you")
    if total == 0 and not signals:
        print("[ok] Nothing pending.")


# ── Log ───────────────────────────────────────────────────────

def cmd_log(state: State, limit: int = 20):
    log_data = state.read("log")
    entries = log_data[-limit:]
    if _json_mode:
        return _emit_json({"command": "log", "entries": entries})
    if not log_data:
        print("No activity yet.")
        return
    print(f"Activity Log (last {len(entries)}):\n")
    for e in entries:
        print(f"  [{short_time(e['at'])}] {e['summary']}")


# ── Request ───────────────────────────────────────────────────

def cmd_request(state: State, from_node: str, to_node: str, description: str):
    _touch_heartbeat(state, from_node)
    nodes = state.read("nodes")
    if to_node not in nodes:
        active = ", ".join(sorted(nodes.keys())) or "(none)"
        print(f'[ERROR] Node "{to_node}" not found')
        print(f'  Active nodes: {active}')
        sys.exit(1)

    # Create an assigned task
    tid = state.next_task_id()
    task = {
        "id": tid, "title": description,
        "description": f"Requested by {from_node}",
        "status": "claimed", "priority": "high",
        "created_by": from_node, "assigned_to": to_node,
        "created_at": utcnow(), "updated_at": utcnow(),
        "result": "",
        "history": [
            {"action": "created", "by": from_node, "at": utcnow()},
            {"action": f"assigned to {to_node}", "by": from_node, "at": utcnow()},
        ],
    }
    def _do_task(tasks):
        tasks[str(tid)] = task
    state.update("tasks", _do_task)

    # Send a message too
    msg = {
        "from": from_node, "to": to_node,
        "content": f"[Request] {description} (task #{tid})",
        "at": utcnow(), "type": "request",
    }
    def _do_msg(messages):
        messages.append(msg)
        while len(messages) > MSG_MAX:
            messages.pop(0)
    state.update("messages", _do_msg)

    signal_node(state.dir, to_node, f"Request from {from_node}: task #{tid}")
    state.append_log(from_node, "request",
                     f'{from_node} requested {to_node}: "{trunc(description, 40)}" (task #{tid})')
    print(f'[OK] Request sent to "{to_node}" as task #{tid}')


# ── Health & Summary ──────────────────────────────────────────

def cmd_health(state: State):
    """Check the health of all nodes — heartbeat age, lock count, task load."""
    nodes = state.read("nodes")
    tasks = state.read("tasks")
    locks = state.read("locks")
    if _json_mode:
        return _emit_json({"command": "health", "nodes": nodes, "tasks": tasks, "locks": locks})
    if not nodes:
        print("No nodes registered.")
        return
    now = datetime.now(timezone.utc)
    print("=== Node Health ===\n")
    for n, info in sorted(nodes.items()):
        hb = info.get("last_heartbeat", info.get("joined_at", ""))
        try:
            hb_age = int((now - parse_ts(hb)).total_seconds())
        except Exception:
            hb_age = -1
        status = info.get("status", "?")
        stale = hb_age > STALE_NODE_SEC if hb_age >= 0 else False
        my_locks = [f for f, v in locks.items() if v["held_by"] == n]
        my_active = [t for t in tasks.values()
                     if t.get("assigned_to") == n and t["status"] in ("active", "claimed")]
        my_done = [t for t in tasks.values()
                   if t.get("assigned_to") == n and t["status"] == "done"]
        health = "STALE" if stale else "OK"
        hb_str = ago(hb) if hb else "never"
        print(f"  {n:<12} [{status:<6}] health={health}  heartbeat={hb_str}  "
              f"tasks={len(my_active)} active/{len(my_done)} done  locks={len(my_locks)}")


def cmd_summary(state: State):
    """Session summary report: completed work, timelines, stats."""
    nodes = state.read("nodes")
    tasks = state.read("tasks")
    messages = state.read("messages")
    log_data = state.read("log")
    locks = state.read("locks")

    if _json_mode:
        return _emit_json({
            "command": "summary",
            "nodes": nodes,
            "tasks": tasks,
            "message_count": len(messages),
            "log_count": len(log_data),
            "lock_count": len(locks),
        })

    print("=" * 52)
    print("   Session Summary")
    print("=" * 52)

    # Task stats
    all_tasks = list(tasks.values())
    done = [t for t in all_tasks if t["status"] == "done"]
    active = [t for t in all_tasks if t["status"] in ("active", "claimed")]
    blocked = [t for t in all_tasks if t["status"] == "blocked"]
    open_t = [t for t in all_tasks if t["status"] == "open"]

    print(f"\n  Tasks: {len(all_tasks)} total — {len(done)} done, "
          f"{len(active)} in progress, {len(blocked)} blocked, {len(open_t)} open")

    # Per-node breakdown
    if nodes:
        print("\n  Per-Node Breakdown:")
        for n in sorted(nodes):
            n_done = [t for t in done if t.get("assigned_to") == n]
            n_active = [t for t in active if t.get("assigned_to") == n]
            print(f"    {n:<12}  {len(n_done)} done, {len(n_active)} in progress")

    # Completed tasks detail
    if done:
        print("\n  Completed Work:")
        for t in sorted(done, key=lambda x: x.get("updated_at", "")):
            result = trunc(t.get("result", ""), 50)
            who = t.get("assigned_to", "?")
            print(f"    #{t['id']} ({who}): {trunc(t['title'], 35)} -> {result}")

    # Message stats
    print(f"\n  Messages: {len(messages)} total")
    by_type = {}
    for m in messages:
        mt = m.get("type", "direct")
        by_type[mt] = by_type.get(mt, 0) + 1
    for mt, count in sorted(by_type.items()):
        print(f"    {mt}: {count}")

    # Active locks
    if locks:
        print(f"\n  Active Locks: {len(locks)}")
        for fp, info in locks.items():
            print(f"    {fp} -> {info['held_by']} ({ago(info.get('acquired_at', ''))})")

    print()


# ── Window Control Commands ───────────────────────────────────

def cmd_inject(state: State, target_node: str, prompt: str):
    """Type a prompt into the target node's terminal and press Enter."""
    session = find_collab_window(target_node)
    if not session:
        backend_name = _injection_backend.name if _injection_backend else "none"
        print(f'[ERROR] No console found for "{target_node}" (backend: {backend_name})')
        sys.exit(1)

    print(f'  Found {target_node}: session {session} (via {_injection_backend.name})')
    print(f'  Injecting: {trunc(prompt, 80)}')

    if _run_inject(target_node, prompt):
        state.append_log("lead", "inject",
                         f'Injected prompt to {target_node}: "{trunc(prompt, 40)}"')
        print(f'[OK] Prompt sent to "{target_node}"')
    else:
        print(f'[ERROR] Failed to inject into "{target_node}"')
        sys.exit(1)


def cmd_interrupt(state: State, target_node: str):
    """Send Escape to the target node's console to stop generation."""
    session = find_collab_window(target_node)
    if not session:
        print(f'[ERROR] No console found for "{target_node}"')
        sys.exit(1)

    print(f'  Found {target_node}: session {session} (via {_injection_backend.name})')

    if _run_interrupt(target_node):
        state.append_log("lead", "interrupt", f'Interrupted {target_node} (sent Escape)')
        print(f'[OK] Escape sent to "{target_node}"')
    else:
        print(f'[ERROR] Failed to send Escape to "{target_node}"')
        sys.exit(1)


def cmd_nudge(state: State, target_node: str, message: str = ""):
    """Send a signal + inject a poll command into the target's console."""
    # Always write a signal file
    reason = message if message else "Nudge from lead"
    signal_node(state.dir, target_node, reason)

    # If a message was provided, send it via collab system too
    if message:
        msg = {
            "from": "lead", "to": target_node,
            "content": message, "at": utcnow(), "type": "nudge",
        }
        def _do(messages):
            messages.append(msg)
            while len(messages) > MSG_MAX:
                messages.pop(0)
        state.update("messages", _do)

    session = find_collab_window(target_node)
    if session:
        backend_name = _injection_backend.name if _injection_backend else "unknown"
        print(f'  Found {target_node}: session {session} (via {backend_name})')
        # Inject a poll command so they see their updates
        collab_path = str(SCRIPT_PATH).replace("\\", "/")
        poll_cmd = f'python "{collab_path}" poll {target_node}'
        if _run_inject(target_node, poll_cmd):
            state.append_log("lead", "nudge", f'Nudged {target_node} (console + signal)')
            print(f'[OK] Nudged "{target_node}" (signal + poll injected)')
        else:
            state.append_log("lead", "nudge", f'Nudged {target_node} (signal only, inject failed)')
            print(f'[WARN] Signal sent but console injection failed for "{target_node}"')
    else:
        state.append_log("lead", "nudge", f'Nudged {target_node} (signal only, no console)')
        print(f'[WARN] Signal sent but no console found for "{target_node}"')
        print(f'  The node will see the signal next time it runs "pending"')


def cmd_windows(state: State):
    """List all detectable collaboration consoles."""
    all_sessions = list_all_sessions()
    nodes = state.read("nodes") or {}
    backend_name = _injection_backend.name if _injection_backend else "none"

    if _json_mode:
        return _emit_json({
            "command": "windows",
            "backend": backend_name,
            "sessions": all_sessions,
            "registered_nodes": list(nodes.keys()),
        })

    # Show all known roles — both registered nodes and detected consoles
    all_names = set(nodes.keys()) | set(all_sessions.keys())
    if not all_names:
        print("No nodes registered and no consoles detected.")
        print(f"  Backend: {backend_name}")
        return
    print(f"Collaboration Consoles (backend: {backend_name}):\n")
    for name in sorted(all_names):
        info = all_sessions.get(name)
        registered = name in nodes
        if info:
            tag = "[FOUND]" if registered else "[FOUND - not joined]"
            print(f"  {name:<12} {tag}  {info['backend']} session {info['session']}")
        else:
            print(f"  {name:<12} [NOT FOUND]")


# ── Reset ─────────────────────────────────────────────────────

def cmd_validate(state: State, repair: bool = False):
    """Validate state file integrity and optionally repair issues."""
    issues = []
    repairs = []

    for name, default in _DEFAULTS.items():
        path = state.dir / f"{name}.json"
        if not path.exists():
            issues.append(f"  MISSING: {name}.json")
            if repair:
                state._write_raw(path, default)
                repairs.append(f"  REPAIRED: {name}.json (recreated with defaults)")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            issues.append(f"  CORRUPT: {name}.json ({e})")
            if repair:
                state._write_raw(path, default)
                repairs.append(f"  REPAIRED: {name}.json (reset to defaults)")
            continue

        # Type check
        expected = type(default)
        if not isinstance(data, expected):
            issues.append(f"  WRONG TYPE: {name}.json (expected {expected.__name__}, got {type(data).__name__})")
            if repair:
                state._write_raw(path, default)
                repairs.append(f"  REPAIRED: {name}.json (reset to defaults)")
            continue

    # Validate task references
    tasks = state.read("tasks")
    nodes = state.read("nodes")
    for tid, task in tasks.items():
        # Check task has required fields
        for field in ("id", "title", "status", "created_at"):
            if field not in task:
                issues.append(f"  TASK #{tid}: missing field '{field}'")
        # Check assignee exists
        assignee = task.get("assigned_to")
        if assignee and assignee not in nodes:
            issues.append(f"  TASK #{tid}: assigned to '{assignee}' who is not a registered node")
        # Check dependencies reference valid tasks
        for dep in task.get("depends_on", []):
            if str(dep) not in tasks:
                issues.append(f"  TASK #{tid}: depends on #{dep} which does not exist")

    # Check for orphaned lock files
    for p in state.dir.glob("*.lock"):
        lock_age = time.time() - p.stat().st_mtime
        if lock_age > STALE_LOCK_SEC * 2:
            issues.append(f"  STALE LOCK FILE: {p.name} ({int(lock_age)}s old)")
            if repair:
                p.unlink()
                repairs.append(f"  REPAIRED: removed stale lock file {p.name}")

    # Check for orphaned signal files
    for p in state.dir.glob("_signal_*"):
        sig_age = time.time() - p.stat().st_mtime
        if sig_age > 3600:  # 1 hour
            issues.append(f"  STALE SIGNAL: {p.name} ({int(sig_age)}s old)")
            if repair:
                p.unlink()
                repairs.append(f"  REPAIRED: removed stale signal file {p.name}")

    if not issues:
        print("[OK] All state files valid. No issues found.")
        return

    print(f"=== Validation Report ({len(issues)} issue(s)) ===\n")
    for issue in issues:
        print(issue)
    if repairs:
        print(f"\n=== Repairs ({len(repairs)}) ===\n")
        for r in repairs:
            print(r)
    elif issues and not repair:
        print(f"\n  Run with --repair to auto-fix {len(issues)} issue(s)")


_COLLAB_MARKER = "<!-- COLLAB:AUTO -->"

def cmd_cleanup(state: State, project_dir: str = ""):
    """Remove collaboration instructions from CLAUDE.md, restoring it to pre-session state."""
    import re as _re
    # Determine project directory
    if project_dir:
        pdir = Path(project_dir).resolve()
    else:
        pdir = Path.cwd()

    claude_md = pdir / "CLAUDE.md"
    if not claude_md.exists():
        print("[OK] No CLAUDE.md found — nothing to clean up")
        return

    content = claude_md.read_text(encoding="utf-8")
    if _COLLAB_MARKER not in content:
        print("[OK] CLAUDE.md has no collaboration section — nothing to clean up")
        return

    # Remove the auto-generated section (between markers)
    pattern = _re.compile(
        rf"\n*{_re.escape(_COLLAB_MARKER)}.*?{_re.escape(_COLLAB_MARKER)}\n*",
        _re.DOTALL,
    )
    cleaned = pattern.sub("\n", content).strip()

    if cleaned:
        claude_md.write_text(cleaned + "\n", encoding="utf-8")
        print(f"[OK] Removed collaboration section from {claude_md}")
        print("     Your original CLAUDE.md content is preserved")
    else:
        # If CLAUDE.md was entirely the collab section, remove the file
        claude_md.unlink()
        print(f"[OK] Removed {claude_md} (it only contained collaboration instructions)")


def cmd_reset(state: State, confirm: bool = False):
    if not confirm:
        print("[ERROR] This will delete ALL collaboration state.")
        print("        Use --confirm to proceed.")
        sys.exit(1)
    shutil.rmtree(state.dir, ignore_errors=True)
    state.__init__(state.dir)
    print("[OK] All collaboration state has been cleared")


# ══════════════════════════════════════════════════════════════
#  CLI PARSER
# ══════════════════════════════════════════════════════════════

# ── Command Aliases ──────────────────────────────────────────
# Short aliases for frequent commands — saves tokens for Claude instances.

ALIASES = {
    "s":  "status",
    "p":  "poll",
    "pd": "pending",
    "b":  "broadcast",
    "t":  "task",
    "c":  "context",
    "h":  "health",
    "w":  "windows",
    "n":  "nudge",
}


def _expand_aliases(argv: list) -> list:
    """Expand command aliases in sys.argv before argparse sees them."""
    if not argv:
        return argv
    # Find the first non-flag argument (skip --state-dir, --version, etc.)
    for i, arg in enumerate(argv):
        if not arg.startswith("-"):
            if arg in ALIASES:
                argv = argv[:i] + [ALIASES[arg]] + argv[i + 1:]
            break
    return argv


def build_parser() -> argparse.ArgumentParser:
    alias_help = "  ".join(f"{k}={v}" for k, v in sorted(ALIASES.items()))
    p = argparse.ArgumentParser(
        prog="collab",
        description="Claude Code Collaboration Harness -- real-time multi-instance coordination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
aliases:
  {alias_help}

examples:
  collab join architect --role "system design"
  collab s                                             # status (alias)
  collab p coder                                       # poll (alias)
  collab b architect "Database migration complete"     # broadcast (alias)
  collab t add "Implement auth" --assign coder --priority high --by architect
  collab task comment 3 "Looking good" --by reviewer
  collab context set "db_type" "postgresql" --by architect
  collab health
  collab summary
""",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--state-dir", default=None, help="Override state directory path")
    p.add_argument("--json", action="store_true", default=False,
                   help="Output structured JSON instead of human-readable text")
    p.add_argument("--brief", action="store_true", default=False,
                   help="Compact output — saves context window tokens")
    sub = p.add_subparsers(dest="command", help="Available commands")

    # ── join ──
    j = sub.add_parser("join", help="Join the collaboration session")
    j.add_argument("name", help="Unique node name (e.g. architect, backend, frontend)")
    j.add_argument("--role", default="general", help="Role description")

    # ── leave ──
    lv = sub.add_parser("leave", help="Leave the collaboration (releases your locks)")
    lv.add_argument("name", help="Your node name")

    # ── status ──
    st = sub.add_parser("status", help="Full overview: nodes, tasks, context, locks, activity")
    st.add_argument("--compact", action="store_true",
                    help="Dense single-line-per-item output (saves tokens)")

    # ── health ──
    sub.add_parser("health", help="Check health of all nodes (heartbeat, locks, tasks)")

    # ── summary ──
    sub.add_parser("summary", help="Session summary report (completed work, stats)")

    # ── heartbeat ──
    hb = sub.add_parser("heartbeat", help="Update your working status")
    hb.add_argument("name", help="Your node name")
    hb.add_argument("--working-on", dest="working_on", default=None,
                    help="What you're currently doing")
    hb.add_argument("--status", dest="node_status", default=None,
                    choices=["active", "idle", "busy", "away"])

    # ── send ──
    sd = sub.add_parser("send", help="Send a direct message")
    sd.add_argument("from_node", metavar="from", help="Your node name")
    sd.add_argument("to", help="Target node name")
    sd.add_argument("message", help="Message content")

    # ── broadcast ──
    bc = sub.add_parser("broadcast", help="Message all nodes")
    bc.add_argument("from_node", metavar="from", help="Your node name")
    bc.add_argument("message", help="Message content")

    # ── inbox ──
    ib = sub.add_parser("inbox", help="View your messages")
    ib.add_argument("name", help="Your node name")
    ib.add_argument("--all", action="store_true", dest="show_all",
                    help="Show all messages, not just since last poll")
    ib.add_argument("--limit", type=int, default=20, help="Max messages")

    # ── context ──
    cx = sub.add_parser("context", help="Shared key-value context store")
    cx_sub = cx.add_subparsers(dest="context_cmd")

    cs = cx_sub.add_parser("set", help="Set a context value")
    cs.add_argument("key")
    cs.add_argument("value")
    cs.add_argument("--by", default="system", help="Your node name")

    cg = cx_sub.add_parser("get", help="Get context value(s)")
    cg.add_argument("key", nargs="?", default=None, help="Key (omit for all)")

    cd = cx_sub.add_parser("del", help="Delete a context key")
    cd.add_argument("key")

    ca = cx_sub.add_parser("append", help="Append to an existing context value")
    ca.add_argument("key")
    ca.add_argument("value")
    ca.add_argument("--by", default="system", help="Your node name")

    # ── task ──
    tk = sub.add_parser("task", help="Shared task board")
    tk_sub = tk.add_subparsers(dest="task_cmd")

    ta = tk_sub.add_parser("add", help="Create a new task")
    ta.add_argument("title")
    ta.add_argument("--desc", default="", help="Detailed description")
    ta.add_argument("--assign", default="", help="Assign to a node")
    ta.add_argument("--priority", default="medium",
                    choices=["low", "medium", "high", "critical"])
    ta.add_argument("--depends-on", dest="depends_on", default="",
                    help="Comma-separated task IDs this depends on")
    ta.add_argument("--by", default="system", help="Your node name")

    tl = tk_sub.add_parser("list", help="List tasks")
    tl.add_argument("--status", default=None, help="Filter: open/claimed/active/done/blocked")
    tl.add_argument("--assigned", default=None, help="Filter by assignee")

    tc = tk_sub.add_parser("claim", help="Claim an open task")
    tc.add_argument("name", help="Your node name")
    tc.add_argument("task_id", type=int, help="Task ID")

    tu = tk_sub.add_parser("update", help="Update task status")
    tu.add_argument("task_id", type=int)
    tu.add_argument("new_status",
                    choices=["open", "claimed", "active", "done", "blocked"])
    tu.add_argument("--result", default="", help="Result or notes")
    tu.add_argument("--by", default="system", help="Your node name")

    ts = tk_sub.add_parser("show", help="Show full task details")
    ts.add_argument("task_id", type=int)

    tcm = tk_sub.add_parser("comment", help="Add a comment to a task")
    tcm.add_argument("task_id", type=int)
    tcm.add_argument("text", help="Comment text")
    tcm.add_argument("--by", default="system", help="Your node name")

    tra = tk_sub.add_parser("reassign", help="Reassign a task to a different node")
    tra.add_argument("task_id", type=int)
    tra.add_argument("new_assignee", help="Node to reassign to")
    tra.add_argument("--by", default="system", help="Your node name")

    # ── lock / unlock / locks ──
    lk = sub.add_parser("lock", help="Lock a file before editing")
    lk.add_argument("name", help="Your node name")
    lk.add_argument("file", help="File path to lock")

    ul = sub.add_parser("unlock", help="Release a file lock")
    ul.add_argument("name", help="Your node name")
    ul.add_argument("file", help="File path to unlock")

    sub.add_parser("locks", help="List all active file locks")

    # ── pending ──
    pd = sub.add_parser("pending", help="Quick check: any signals, messages, or tasks waiting?")
    pd.add_argument("name", help="Your node name")

    # ── poll ──
    pl = sub.add_parser("poll", help="Get all updates since your last poll")
    pl.add_argument("name", help="Your node name")

    # ── log ──
    lg = sub.add_parser("log", help="View the activity log")
    lg.add_argument("--limit", type=int, default=20, help="Number of entries")

    # ── request ──
    rq = sub.add_parser("request", help="Request work from another node (task + message)")
    rq.add_argument("from_node", metavar="from", help="Your node name")
    rq.add_argument("to", help="Target node name")
    rq.add_argument("description", help="What you need done")

    # ── window control ──
    inj = sub.add_parser("inject", help="Type a prompt into a node's terminal window")
    inj.add_argument("target", help="Target node name (e.g. dev1)")
    inj.add_argument("prompt", help="Text to type (will press Enter after)")

    intr = sub.add_parser("interrupt", help="Send Escape to a node's window (stop generation)")
    intr.add_argument("target", help="Target node name")

    ndg = sub.add_parser("nudge", help="Signal + inject a poll command into a node's window")
    ndg.add_argument("target", help="Target node name")
    ndg.add_argument("message", nargs="?", default="", help="Optional message to send")

    sub.add_parser("windows", help="List all detectable collaboration windows")

    # ── whoami ──
    wh = sub.add_parser("whoami", help="Print the role banner to identify this terminal")
    wh.add_argument("name", help="Your node name")

    # ── validate ──
    vl = sub.add_parser("validate", help="Validate state file integrity")
    vl.add_argument("--repair", action="store_true", help="Auto-fix detected issues")

    # ── cleanup ──
    cl = sub.add_parser("cleanup", help="Remove collaboration instructions from CLAUDE.md")
    cl.add_argument("--project-dir", default="",
                    help="Project directory (default: current directory)")

    # ── reset ──
    rs = sub.add_parser("reset", help="Clear ALL collaboration state")
    rs.add_argument("--confirm", action="store_true", help="Required to confirm reset")

    return p


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    # Expand aliases before parsing
    sys.argv[1:] = _expand_aliases(sys.argv[1:])

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    global _json_mode, _brief_mode
    _json_mode = args.json
    _brief_mode = args.brief

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    state = State(state_dir)

    try:
        cmd = args.command

        if cmd == "join":
            cmd_join(state, args.name, args.role)
        elif cmd == "leave":
            cmd_leave(state, args.name)
        elif cmd == "status":
            cmd_status(state, compact=args.compact)
        elif cmd == "health":
            cmd_health(state)
        elif cmd == "summary":
            cmd_summary(state)
        elif cmd == "heartbeat":
            cmd_heartbeat(state, args.name, args.working_on, args.node_status)
        elif cmd == "send":
            cmd_send(state, args.from_node, args.to, args.message)
        elif cmd == "broadcast":
            cmd_broadcast(state, args.from_node, args.message)
        elif cmd == "inbox":
            cmd_inbox(state, args.name, args.show_all, args.limit)
        elif cmd == "context":
            cc = args.context_cmd
            if not cc:
                print("[ERROR] Missing subcommand for `context`")
                print('  Usage: context {set|get|del|append} ...')
                print('  Example: context set "db" "postgres" --by alice')
                sys.exit(1)
            {"set":    lambda: cmd_context_set(state, args.key, args.value, args.by),
             "get":    lambda: cmd_context_get(state, args.key),
             "del":    lambda: cmd_context_del(state, args.key),
             "append": lambda: cmd_context_append(state, args.key, args.value, args.by),
            }[cc]()
        elif cmd == "task":
            tc = args.task_cmd
            if not tc:
                print("[ERROR] Missing subcommand for `task`")
                print('  Usage: task {add|list|claim|update|show|comment|reassign} ...')
                print('  Example: task add "Fix bug" --assign dev1 --by lead')
                sys.exit(1)
            {"add":      lambda: cmd_task_add(state, args.title, args.desc, args.assign,
                                              args.priority, args.by, args.depends_on),
             "list":     lambda: cmd_task_list(state, args.status, args.assigned),
             "claim":    lambda: cmd_task_claim(state, args.name, args.task_id),
             "update":   lambda: cmd_task_update(state, args.task_id, args.new_status,
                                                  args.result, args.by),
             "show":     lambda: cmd_task_show(state, args.task_id),
             "comment":  lambda: cmd_task_comment(state, args.task_id, args.text, args.by),
             "reassign": lambda: cmd_task_reassign(state, args.task_id, args.new_assignee, args.by),
            }[tc]()
        elif cmd == "lock":
            cmd_lock(state, args.name, args.file)
        elif cmd == "unlock":
            cmd_unlock(state, args.name, args.file)
        elif cmd == "locks":
            cmd_locks(state)
        elif cmd == "pending":
            cmd_pending(state, args.name)
        elif cmd == "poll":
            cmd_poll(state, args.name)
        elif cmd == "log":
            cmd_log(state, args.limit)
        elif cmd == "request":
            cmd_request(state, args.from_node, args.to, args.description)
        elif cmd == "inject":
            cmd_inject(state, args.target, args.prompt)
        elif cmd == "interrupt":
            cmd_interrupt(state, args.target)
        elif cmd == "nudge":
            cmd_nudge(state, args.target, args.message)
        elif cmd == "windows":
            cmd_windows(state)
        elif cmd == "whoami":
            cmd_whoami(state, args.name)
        elif cmd == "validate":
            cmd_validate(state, args.repair)
        elif cmd == "cleanup":
            cmd_cleanup(state, args.project_dir)
        elif cmd == "reset":
            cmd_reset(state, args.confirm)

    except TimeoutError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
