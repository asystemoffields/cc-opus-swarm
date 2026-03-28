#!/usr/bin/env python3
"""
Cross-Platform Terminal Injection
==================================
Provides inject/interrupt/detect capabilities across:
  - Windows: Win32 AttachConsole + WriteConsoleInputW (via subprocess helper)
  - Linux/macOS (tmux): tmux send-keys
  - Linux/macOS (screen): screen -X stuff

This module is imported by collab.py to replace the Windows-only injection code.
Zero external dependencies — Python 3.12+ stdlib only.

Usage:
    from inject import get_backend, list_sessions

    backend = get_backend()       # auto-detect best available backend
    sessions = backend.list_sessions()  # find collaboration sessions
    backend.inject(target, "hello world")
    backend.interrupt(target)
"""

import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod

# ── Constants ─────────────────────────────────────────────────

# Role names that the launcher creates bat/shell scripts for
_ALL_ROLES = ["lead"] + [f"dev{i}" for i in range(1, 20)]


# ══════════════════════════════════════════════════════════════
#  Abstract Backend
# ══════════════════════════════════════════════════════════════

class InjectionBackend(ABC):
    """Base class for platform-specific terminal injection."""

    name: str = "abstract"

    @abstractmethod
    def available(self) -> bool:
        """Return True if this backend can be used on the current system."""

    @abstractmethod
    def list_sessions(self) -> dict[str, str]:
        """Return {role_name: session_id} for detected collaboration sessions."""

    @abstractmethod
    def inject(self, target: str, text: str) -> bool:
        """Type text + Enter into the target's terminal. Returns True on success."""

    @abstractmethod
    def interrupt(self, target: str) -> bool:
        """Send Escape (x2) to the target's terminal. Returns True on success."""

    def find_target(self, role_name: str) -> str | None:
        """Find the session/PID for a role. Returns session_id or None."""
        sessions = self.list_sessions()
        return sessions.get(role_name)


# ══════════════════════════════════════════════════════════════
#  Windows Backend (Win32 API)
# ══════════════════════════════════════════════════════════════

# Injector script spawned as a subprocess so we don't disturb our own console.
_WIN32_INJECTOR = r'''
import ctypes, ctypes.wintypes, sys, time

KEY_EVENT = 0x0001
GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ  = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = -1

kernel32 = ctypes.windll.kernel32

class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.wintypes.BOOL),
        ("wRepeatCount", ctypes.wintypes.WORD),
        ("wVirtualKeyCode", ctypes.wintypes.WORD),
        ("wVirtualScanCode", ctypes.wintypes.WORD),
        ("uChar", ctypes.wintypes.WCHAR),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
    ]

class INPUT_RECORD_Event(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", ctypes.wintypes.WORD),
        ("_padding", ctypes.wintypes.WORD),
        ("Event", INPUT_RECORD_Event),
    ]

def write_key(handle, char, vk=0):
    written = ctypes.wintypes.DWORD()
    for down in (True, False):
        rec = INPUT_RECORD()
        rec.EventType = KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = down
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.wVirtualScanCode = 0
        rec.Event.KeyEvent.uChar = char
        rec.Event.KeyEvent.dwControlKeyState = 0
        ok = kernel32.WriteConsoleInputW(handle, ctypes.byref(rec), 1, ctypes.byref(written))
        if not ok:
            err = ctypes.get_last_error()
            print(f"WriteConsoleInputW failed: error={err}", file=sys.stderr)
            return False
    return True

def open_conin():
    handle = kernel32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        print(f"CreateFileW(CONIN$) failed: error={err}", file=sys.stderr)
        return None
    return handle

def main():
    target_pid = int(sys.argv[1])
    action = sys.argv[2]          # "text", "escape", or "enter"
    payload = sys.argv[3] if len(sys.argv) > 3 else ""

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(target_pid):
        err = ctypes.get_last_error()
        print(f"AttachConsole failed for PID {target_pid} (error {err})", file=sys.stderr)
        sys.exit(1)

    handle = open_conin()
    if handle is None:
        kernel32.FreeConsole()
        sys.exit(1)

    if action == "escape":
        write_key(handle, '\x1b', 0x1B)
        time.sleep(0.05)
        write_key(handle, '\x1b', 0x1B)
    elif action == "text":
        for ch in payload:
            if ch == '\n':
                write_key(handle, '\r', 0x0D)
            else:
                write_key(handle, ch, 0)
            time.sleep(0.003)
        time.sleep(0.02)
        write_key(handle, '\r', 0x0D)
    elif action == "enter":
        write_key(handle, '\r', 0x0D)

    kernel32.CloseHandle(handle)
    kernel32.FreeConsole()
    print("OK")

if __name__ == "__main__":
    main()
'''


class WindowsBackend(InjectionBackend):
    """Windows injection via AttachConsole + WriteConsoleInputW."""

    name = "win32"

    def available(self) -> bool:
        return sys.platform == "win32"

    def list_sessions(self) -> dict[str, str]:
        """Find cmd.exe PIDs for each role by matching _run_<role>.bat in command lines."""
        ps_cmd = (
            'Get-CimInstance Win32_Process -Filter "Name=\'cmd.exe\'" '
            '| Select-Object ProcessId,CommandLine '
            '| ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }'
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=8,
            )
        except Exception:
            return {}

        role_pids = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid_str, _, cmdline = line.partition("|")
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            for role in _ALL_ROLES:
                if f"_run_{role}.bat" in cmdline:
                    role_pids[role] = str(pid)
                    break
        return role_pids

    def _run_injector(self, pid: int, action: str, payload: str = "") -> bool:
        """Spawn the injector helper to write to another console."""
        try:
            result = subprocess.run(
                [sys.executable, "-c", _WIN32_INJECTOR,
                 str(pid), action, payload],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if stderr:
                    print(f"  [injector error] {stderr}")
                return False
            return "OK" in result.stdout
        except Exception as e:
            print(f"  [injector error] {e}")
            return False

    def inject(self, target: str, text: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        return self._run_injector(int(session), "text", text)

    def interrupt(self, target: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        return self._run_injector(int(session), "escape")


# ══════════════════════════════════════════════════════════════
#  tmux Backend (Linux / macOS)
# ══════════════════════════════════════════════════════════════

class TmuxBackend(InjectionBackend):
    """tmux-based injection for Linux/macOS."""

    name = "tmux"

    # Session/window naming convention: collab_<role>
    _PREFIX = "collab_"

    def available(self) -> bool:
        if sys.platform == "win32":
            return False
        return shutil.which("tmux") is not None

    def list_sessions(self) -> dict[str, str]:
        """Find tmux windows/panes matching collab_<role> naming."""
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-a", "-F",
                 "#{session_name}:#{window_index} #{window_name}"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return {}

        if result.returncode != 0:
            return {}

        sessions = {}
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            target, window_name = parts
            for role in _ALL_ROLES:
                if window_name == f"{self._PREFIX}{role}" or window_name == role:
                    sessions[role] = target
                    break
        return sessions

    def inject(self, target: str, text: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        try:
            # send-keys types the text literally, then Enter
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, text, "Enter"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def interrupt(self, target: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        try:
            # Send Escape twice
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "Escape"],
                capture_output=True, text=True, timeout=5,
            )
            time.sleep(0.05)
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, "Escape"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════
#  GNU Screen Backend (Linux / macOS)
# ══════════════════════════════════════════════════════════════

class ScreenBackend(InjectionBackend):
    """GNU Screen-based injection for Linux/macOS."""

    name = "screen"

    # Session naming convention: collab_<role>
    _PREFIX = "collab_"

    def available(self) -> bool:
        if sys.platform == "win32":
            return False
        return shutil.which("screen") is not None

    def list_sessions(self) -> dict[str, str]:
        """Find screen sessions matching collab_<role> naming."""
        try:
            result = subprocess.run(
                ["screen", "-ls"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return {}

        sessions = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            for role in _ALL_ROLES:
                session_name = f"{self._PREFIX}{role}"
                if session_name in line:
                    # Extract the full session ID (e.g., "12345.collab_dev1")
                    parts = line.split("\t")
                    if parts:
                        sid = parts[0].strip()
                        sessions[role] = sid
                    break
        return sessions

    def inject(self, target: str, text: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        try:
            # screen -S <session> -X stuff "text\n"
            result = subprocess.run(
                ["screen", "-S", session, "-X", "stuff", text + "\n"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def interrupt(self, target: str) -> bool:
        session = self.find_target(target)
        if not session:
            return False
        try:
            # Send Escape twice via screen stuff
            subprocess.run(
                ["screen", "-S", session, "-X", "stuff", "\x1b"],
                capture_output=True, text=True, timeout=5,
            )
            time.sleep(0.05)
            result = subprocess.run(
                ["screen", "-S", session, "-X", "stuff", "\x1b"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════
#  Backend Selection
# ══════════════════════════════════════════════════════════════

# Priority order: platform-native first, then multiplexers
_BACKENDS = [WindowsBackend, TmuxBackend, ScreenBackend]


def get_backend() -> InjectionBackend | None:
    """Auto-detect and return the best available injection backend.
    Returns None if no backend is available."""
    for cls in _BACKENDS:
        backend = cls()
        if backend.available():
            return backend
    return None


def get_all_backends() -> list[InjectionBackend]:
    """Return all available backends (for diagnostics)."""
    return [cls() for cls in _BACKENDS if cls().available()]


def list_all_sessions() -> dict[str, dict]:
    """Find all sessions across all available backends.
    Returns {role: {"backend": name, "session": id}}."""
    result = {}
    for cls in _BACKENDS:
        backend = cls()
        if not backend.available():
            continue
        for role, session in backend.list_sessions().items():
            if role not in result:  # first backend wins
                result[role] = {"backend": backend.name, "session": session}
    return result
