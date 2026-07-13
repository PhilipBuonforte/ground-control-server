"""
Ground Control — server-owned Claude Code sessions.

The old delivery model reached a session by (a) an MCP "channel" the session had
to opt into, (b) typing into the Claude desktop app over Accessibility, or (c)
killing the live process and re-running `claude -p --resume`. All three were
fragile — the channel only existed for `gc`-launched terminals, the AX path
couldn't see the desktop app's composer, and kill-resume interrupts live work.

This module makes Ground Control *own* each session: it spawns `claude` in
persistent stream-json mode and holds its stdin. Sending a message is a single
line written to stdin — 100% reliable, no channel, no AX, no kill/resume. The
child still writes its normal transcript to ~/.claude/projects/.../<sid>.jsonl,
so every existing read/render/poll path in server.py keeps working unchanged.

Design notes:
  * One `ManagedSession` == one long-lived `claude` process. Threads (not async)
    to match the rest of server.py.
  * `send()` guarantees an owned, live process (spawning/resuming as needed) and
    writes the user message. Images are sent as real content blocks.
  * Turn state is tracked from the stdout event stream: busy flips True on send
    and False on the matching `result` event. `on_result`/`on_idle` callbacks let
    server.py arm alerts without the old Stop-hook heuristics.
  * The child owns transcript persistence; we never write the jsonl ourselves.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

# Where we record which session ids Ground Control currently owns (pid + start),
# so `live_sessions()` in server.py can merge owned sessions into its liveness
# view and a crashed server can reap stale registrations on next boot.
OWNED_DIR = Path.home() / ".ground-control" / "owned"


def _log_noop(_msg: str) -> None:
    pass


class ManagedSession:
    """One owned `claude` process in persistent stream-json mode."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        claude_bin: str,
        model: Optional[str] = None,
        resume: bool = False,
        log: Callable[[str], None] = _log_noop,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.session_id = session_id
        self.cwd = cwd if cwd and os.path.isdir(cwd) else str(Path.home())
        self.claude_bin = claude_bin
        self.model = model
        self.log = log
        self.on_event = on_event  # (session_id, event_dict) for every stdout event

        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self.busy = False            # a turn is in flight (sent → result)
        self.last_activity = 0.0     # last send/result — drives idle reaping
        self.last_result_at = 0.0
        self.last_error: Optional[str] = None
        self._pending_turns = 0      # user messages sent but not yet resulted
        self._stdin_lock = threading.Lock()
        self._alive = False
        # Live activity feed — the exact thing this headless process is doing,
        # so the app can render a real-time "watch it work" view (the Live view).
        self.activity = deque(maxlen=400)
        self.activity_seq = 0

    def _push_activity(self, kind: str, text: str) -> None:
        self.activity_seq += 1
        self.activity.append({
            "seq": self.activity_seq, "t": time.time(),
            "kind": kind, "text": (text or "")[:2000],
        })

    @staticmethod
    def _tool_summary(name: str, inp: dict) -> str:
        inp = inp or {}
        if name == "Bash":
            return "$ " + str(inp.get("command", ""))[:200]
        if name in ("Read", "Edit", "Write", "NotebookEdit"):
            return f"{name} {inp.get('file_path') or inp.get('notebook_path') or ''}"
        if name in ("Grep", "Glob"):
            return f"{name} {inp.get('pattern','')}" + (f" in {inp.get('path')}" if inp.get('path') else "")
        if name == "Task" or name == "Agent":
            return f"→ subagent: {inp.get('description') or inp.get('subagent_type') or ''}"
        if name.startswith("mcp__"):
            return "tool " + name.split("__")[-1]
        return name or "tool"

    # -- lifecycle ---------------------------------------------------------

    def _build_cmd(self, resume: bool) -> list[str]:
        cmd = [
            self.claude_bin,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if resume:
            cmd += ["--resume", self.session_id]
        else:
            cmd += ["--session-id", self.session_id]
        return cmd

    def start(self, resume: bool) -> None:
        cmd = self._build_cmd(resume)
        self.log(f"[owned] spawn {self.session_id[:8]} resume={resume} cwd={self.cwd}")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.started_at = time.time()
        self.last_activity = self.started_at
        self._alive = True
        self.last_error = None
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._write_registration()

    def is_live(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def _drain_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = ev.get("type")
            # ---- live activity feed (what the process is doing right now) ----
            try:
                if typ == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text" and (b.get("text") or "").strip():
                            self._push_activity("text", b["text"])
                        elif bt == "tool_use":
                            self._push_activity("tool", self._tool_summary(b.get("name", ""), b.get("input")))
                        elif bt == "thinking":
                            self._push_activity("thinking", "thinking…")
                elif typ == "result":
                    self._push_activity("done", "✓ turn complete")
            except Exception:  # noqa: BLE001 — never let the feed break the drain
                pass
            if typ == "result":
                # A turn finished. Clear busy only when all in-flight turns done.
                self._pending_turns = max(0, self._pending_turns - 1)
                self.last_result_at = time.time()
                self.last_activity = time.time()
                if ev.get("is_error"):
                    self.last_error = ev.get("result") or "unknown error"
                if self._pending_turns == 0:
                    self.busy = False
            if self.on_event:
                try:
                    self.on_event(self.session_id, ev)
                except Exception as e:  # noqa: BLE001
                    self.log(f"[owned] on_event error: {e}")
        # stdout closed → process exiting
        self._alive = False
        self.busy = False
        self._remove_registration()
        self.log(f"[owned] {self.session_id[:8]} stdout closed (exited)")

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self.log(f"[owned:{self.session_id[:8]}:stderr] {line[:200]}")

    # -- messaging ---------------------------------------------------------

    def _envelope(self, text: str, images: Optional[list[str]] = None) -> dict:
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for path in images or []:
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            ext = Path(path).suffix.lower().lstrip(".")
            media = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                     "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f"image/{media}",
                    "data": base64.b64encode(data).decode(),
                },
            })
        if not content:
            content.append({"type": "text", "text": ""})
        return {"type": "user", "message": {"role": "user", "content": content}}

    def send(self, text: str, images: Optional[list[str]] = None) -> None:
        """Write a user message to the live process's stdin."""
        if not self.is_live():
            raise RuntimeError("session process is not live")
        msg = json.dumps(self._envelope(text, images))
        with self._stdin_lock:
            assert self.proc and self.proc.stdin
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
        self._pending_turns += 1
        self.busy = True
        self.last_activity = time.time()

    def interrupt(self) -> bool:
        """Ask the current turn to stop (stream-json control request)."""
        if not self.is_live():
            return False
        ctrl = {
            "type": "control_request",
            "request_id": uuid.uuid4().hex,
            "request": {"subtype": "interrupt"},
        }
        try:
            with self._stdin_lock:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(json.dumps(ctrl) + "\n")
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def close(self, kill: bool = False) -> None:
        """Graceful shutdown: close stdin so the child exits after its turn."""
        if not self.proc:
            return
        try:
            if kill:
                self.proc.kill()
            else:
                if self.proc.stdin:
                    self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        self._remove_registration()

    # -- registration (crash-safe liveness) --------------------------------

    def _reg_file(self) -> Path:
        return OWNED_DIR / f"{self.session_id}.json"

    def _write_registration(self) -> None:
        try:
            OWNED_DIR.mkdir(parents=True, exist_ok=True)
            self._reg_file().write_text(json.dumps({
                "sessionId": self.session_id,
                "pid": self.proc.pid if self.proc else None,
                "cwd": self.cwd,
                "started": self.started_at,
            }))
        except OSError:
            pass

    def _remove_registration(self) -> None:
        try:
            self._reg_file().unlink()
        except OSError:
            pass


class SessionManager:
    """Registry of Ground-Control-owned sessions."""

    def __init__(
        self,
        claude_bin: str,
        default_model: Optional[str] = None,
        log: Callable[[str], None] = _log_noop,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.claude_bin = claude_bin
        self.default_model = default_model
        self.log = log
        self.on_event = on_event
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = threading.Lock()
        # Owned sessions are long-lived processes; close them after this many
        # seconds idle (not busy, no send/result) to avoid piling up `claude`
        # procs. The next send re-adopts seamlessly via `--resume` (~2s respawn).
        self.idle_secs = int(os.environ.get("GC_IDLE_SECS", "1800"))
        self._reap_stale_registrations()
        threading.Thread(target=self._reaper_loop, daemon=True).start()

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.time()
            with self._lock:
                items = list(self._sessions.items())
            for sid, s in items:
                if not s.is_live():
                    with self._lock:
                        self._sessions.pop(sid, None)
                    continue
                if not s.busy and s.last_activity and now - s.last_activity > self.idle_secs:
                    self.log(f"[owned] reaping idle {sid[:8]} "
                             f"(idle {int(now - s.last_activity)}s)")
                    s.close(kill=False)
                    with self._lock:
                        self._sessions.pop(sid, None)

    def _reap_stale_registrations(self) -> None:
        """On boot, delete owned/*.json whose pid is dead (server crashed)."""
        try:
            for f in OWNED_DIR.glob("*.json"):
                try:
                    d = json.loads(f.read_text())
                    pid = d.get("pid")
                    if pid:
                        os.kill(pid, 0)  # alive?
                except (OSError, json.JSONDecodeError):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def get(self, session_id: str) -> Optional[ManagedSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def is_owned_live(self, session_id: str) -> bool:
        s = self.get(session_id)
        return bool(s and s.is_live())

    def busy(self, session_id: str) -> Optional[bool]:
        s = self.get(session_id)
        return s.busy if (s and s.is_live()) else None

    def owned_live_ids(self) -> dict[str, int]:
        """session_id -> pid, for merging into server.py live_sessions()."""
        out: dict[str, int] = {}
        with self._lock:
            for sid, s in self._sessions.items():
                if s.is_live() and s.proc:
                    out[sid] = s.proc.pid
        return out

    def snapshot(self) -> list[dict]:
        """Authoritative live view of every owned headless process — reads the
        actual process + in-memory turn state (NOT the transcript). This is the
        ground truth behind the 'Ground Zero' page."""
        now = time.time()
        out = []
        with self._lock:
            items = list(self._sessions.items())
        for sid, s in items:
            if not s.is_live():
                continue
            out.append({
                "sessionId": sid,
                "pid": s.proc.pid if s.proc else None,
                "busy": bool(s.busy),
                "pending_turns": s._pending_turns,
                "uptime": int(now - s.started_at) if s.started_at else 0,
                "idle": int(now - s.last_activity) if s.last_activity else None,
                "last_result": int(now - s.last_result_at) if s.last_result_at else None,
                "cwd": s.cwd,
                "model": s.model or "",
                "error": s.last_error,
            })
        return out

    def _spawn(self, session_id: str, cwd: str, resume: bool,
               model: Optional[str]) -> ManagedSession:
        s = ManagedSession(
            session_id=session_id,
            cwd=cwd,
            claude_bin=self.claude_bin,
            model=model or self.default_model,
            log=self.log,
            on_event=self.on_event,
        )
        s.start(resume=resume)
        with self._lock:
            self._sessions[session_id] = s
        return s

    def new_session(self, session_id: str, cwd: str, text: str,
                    images: Optional[list[str]] = None,
                    model: Optional[str] = None) -> ManagedSession:
        s = self._spawn(session_id, cwd, resume=False, model=model)
        # small settle so the child has its stdin reader up
        time.sleep(0.4)
        s.send(text, images)
        return s

    def send(self, session_id: str, cwd: str, text: str,
             images: Optional[list[str]] = None,
             model: Optional[str] = None) -> ManagedSession:
        """Ensure an owned, live process for this session and deliver `text`.

        - already owned + live  → write to stdin
        - owned but dead, or not owned yet → resume into a fresh owned process
        """
        s = self.get(session_id)
        if s and s.is_live():
            s.send(text, images)
            return s
        # (re)adopt by resuming the on-disk session into an owned process
        s = self._spawn(session_id, cwd, resume=True, model=model)
        time.sleep(0.4)
        s.send(text, images)
        return s

    def adopt(self, session_id: str, cwd: str,
              model: Optional[str] = None) -> ManagedSession:
        """Take a session into headless ownership WITHOUT sending a message —
        spawn `--resume` and let it idle, ready. Makes any session headless so the
        Live view + sync work on it. If already owned+live, returns the existing one."""
        s = self.get(session_id)
        if s and s.is_live():
            return s
        return self._spawn(session_id, cwd, resume=True, model=model)

    def stop(self, session_id: str) -> bool:
        s = self.get(session_id)
        if not s or not s.is_live():
            return False
        return s.interrupt()

    def shutdown_all(self, kill: bool = False) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.close(kill=kill)
