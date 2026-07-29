"""Pocket Claude — phone dashboard for Claude Code sessions on this Mac.

Lists all sessions from the shared session store, shows conversations,
and injects messages via `claude -p --resume` (auto-releasing any live
desktop-app process first so history never forks).
"""
import glob
import json
import os
import re
import shutil
import signal
import sqlite3
import urllib.parse
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import asyncio

from fastapi import FastAPI, Request, WebSocket, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from gc_sessions import SessionManager
import gc_ez

# ~/.claude is the standard Claude Code data dir (Phil's is a symlink to
# claude-workspace/.claude-global, so this resolves correctly everywhere).
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
STATIC_DIR = Path(__file__).parent / "static"


def _find_claude_bin():
    found = shutil.which("claude")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "claude",
              Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude"),
              Path.home() / ".claude" / "local" / "claude"):
        if c.exists():
            return str(c)
    # npm/nvm installs live under version-specific dirs launchd can't see (its PATH
    # is bare) — a real user's `claude` was invisible to the server this way. Ask the
    # user's LOGIN shell, which has their real PATH.
    try:
        out = subprocess.run(["/bin/zsh", "-lic", "command -v claude"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out and os.path.exists(out.splitlines()[-1]):
            return out.splitlines()[-1]
    except Exception:  # noqa: BLE001
        pass
    # nvm's default layout, direct
    for c in sorted(Path.home().glob(".nvm/versions/node/*/bin/claude"), reverse=True):
        if c.exists():
            return str(c)
    return "claude"


CLAUDE_BIN = _find_claude_bin()
PORT = 8130

app = FastAPI(title="Pocket Claude")

# Ground Control owns each session as a persistent stream-json `claude` process;
# delivery is a line written to stdin (100% reliable — no channel/AX/kill-resume).
GC_MODEL = os.environ.get("GC_MODEL", "claude-opus-4-8")
_sessions = SessionManager(CLAUDE_BIN, default_model=GC_MODEL,
                           log=lambda m: print(m, flush=True))


@app.on_event("startup")
def _startup():
    threading.Thread(target=_alert_worker, daemon=True).start()
    threading.Thread(target=_terminal_work_warmer, daemon=True).start()

# ---------------------------------------------------------------- parsing

_parse_cache = {}  # path -> {mtime, size, offset, lines: [dict], partial: str}
_cache_lock = threading.Lock()


def _read_lines(path: Path):
    """Incrementally parse a jsonl transcript, cached by offset."""
    st = path.stat()
    with _cache_lock:
        c = _parse_cache.get(str(path))
        if c and c["mtime"] == st.st_mtime and c["size"] == st.st_size:
            return c["lines"]
    if c is None or st.st_size < c["size"]:
        c = {"offset": 0, "lines": [], "partial": ""}
    with open(path, "rb") as f:
        f.seek(c["offset"])
        chunk = f.read()
    text = c["partial"] + chunk.decode("utf-8", errors="replace")
    new_partial = ""
    if text and not text.endswith("\n"):
        nl = text.rfind("\n")
        text, new_partial = text[: nl + 1], text[nl + 1 :]
    lines = list(c["lines"])
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    with _cache_lock:
        _parse_cache[str(path)] = {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "offset": c["offset"] + len(chunk),
            "lines": lines,
            "partial": new_partial,
        }
    return lines


def _msg_text(entry):
    """Extract displayable text from a user/assistant transcript entry."""
    if entry.get("isMeta"):
        return None
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    low = text[:40].lower()
    if low.startswith(("<system-reminder", "<command-name", "<local-command", "caveat:", "[request interrupted")):
        return None
    if text.strip() in ("No response requested.", "No response requested"):
        return None
    return text


def _is_turn(entry):
    return (
        entry.get("type") in ("user", "assistant")
        and not entry.get("isSidechain")
        and entry.get("uuid")
    )


import re as _re

_IMG_RE = _re.compile(r"(/[^\s\"'`()\[\]]+?\.(?:png|jpe?g|gif|webp))", _re.IGNORECASE)
_LINE_IMG_RE = _re.compile(r"^[-•*]?\s*(/.+?\.(?:png|jpe?g|gif|webp))\s*$", _re.IGNORECASE | _re.MULTILINE)
_WEB_IMG_RE = _re.compile(r"(https?://[^\s\"'`()\[\]<>]+?\.(?:png|jpe?g|gif|webp))", _re.IGNORECASE)
_ATTACH_RE = _re.compile(r"\n*\[The user attached[^\]]*\]\n(?:- .*\n?)*")


def _extract_images(text):
    """Find image references (local file paths AND web URLs) in a turn."""
    urls, seen = [], set()
    # Remote http(s) image URLs — rendered directly by the app.
    for m in _WEB_IMG_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            urls.append(m)
    # Local file paths — served through the resizing proxy.
    for m in _LINE_IMG_RE.findall(text or "") + _IMG_RE.findall(text or ""):
        if m in seen:
            continue
        if os.path.isfile(m):
            seen.add(m)
            urls.append("/api/file?path=" + urllib.parse.quote(m))
    clean = _ATTACH_RE.sub("", text or "").strip()
    return clean, urls


# Turns the HARNESS writes as role=user. None of these were typed by Phil, so none
# belong in the chat — they were showing up as his own messages.
_NOISE_PREFIXES = (
    "<task-notification>",       # background job finished (the one he caught)
    "<system-reminder>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "caveat: the messages below were generated by the user while running local commands",
    "[request interrupted by user",
    "this session is being continued from a previous conversation",  # compaction preamble
)
# A <system-reminder> can also ride ALONG WITH a real message; strip just that part.
_SYS_BLOCK_RE = _re.compile(r"<system-reminder>.*?</system-reminder>\s*", _re.S | _re.I)


def _is_harness_noise(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t.startswith(p) for p in _NOISE_PREFIXES)


def _strip_system_blocks(text: str) -> str:
    return _SYS_BLOCK_RE.sub("", text or "")


def parse_turns(path: Path):
    """Return conversation turns following the active branch (last leaf → root)."""
    lines = _read_lines(path)
    nodes = {}
    order = {}   # uuid -> file position, for merging off-branch turns in place
    leaf = None
    for i, e in enumerate(lines):
        if e.get("uuid"):
            nodes[e["uuid"]] = e
            order[e["uuid"]] = i
            if _is_turn(e):
                leaf = e
    turns = []
    seen = set()
    cur = leaf
    dir_name = path.parent.name
    sid = path.stem
    while cur is not None and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        if _is_turn(cur):
            text = _msg_text(cur) or ""
            # HARNESS PLUMBING IS NOT A MESSAGE FROM PHIL. Background-task
            # notifications, system reminders and slash-command echoes are written into
            # the transcript with role=user, so the chat rendered them as HIS bubbles —
            # "I didn't send that". Drop them; they're machine-to-machine chatter.
            if cur.get("type") == "user" and _is_harness_noise(text):
                cur = nodes.get(cur.get("parentUuid"))
                continue
            clean, images = _extract_images(_strip_system_blocks(text))
            direct = bool(clean) or bool(images)  # user's own text / inline image
            tool_img = False
            content = (cur.get("message") or {}).get("content")
            if isinstance(content, list):
                for i, b in enumerate(content):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "image":
                        images.append(f"/api/msgimg/{dir_name}/{sid}/{cur['uuid']}/{i}")
                        direct = True
                    elif b.get("type") == "tool_result":
                        # Screenshots the agent takes come back as images nested inside
                        # a tool_result (e.g. Read of a PNG) — index them as "i-j".
                        inner = b.get("content")
                        if isinstance(inner, list):
                            for j, ib in enumerate(inner):
                                if isinstance(ib, dict) and ib.get("type") == "image":
                                    images.append(
                                        f"/api/msgimg/{dir_name}/{sid}/{cur['uuid']}/{i}-{j}")
                                    tool_img = True
            if clean or images:
                # Show the FULL message — no truncation. (The old 2500-char cap made the
                # chat window print "…[truncated]" on any longer message, which Phil
                # rightly flagged: real messages shouldn't be cut.)
                turns.append(
                    {
                        "id": cur["uuid"],
                        "role": cur["type"],
                        "text": clean,
                        "images": images,
                        "ts": cur.get("timestamp"),
                        # Pure tool-result image (no user text/inline image of its own) —
                        # e.g. the agent Read a file. Used to drop the "you sent an image
                        # → the agent read it right back" echo below.
                        "_tool_echo": bool(tool_img and not direct),
                    }
                )
        cur = nodes.get(cur.get("parentUuid"))
    turns.reverse()
    # Collapse the image double: when you send a photo, the agent often Reads it,
    # and that Read comes back as a second, empty user bubble carrying the SAME
    # image — so one send looks like two. Drop a tool-echo image turn when the
    # turn right before it is a user turn that already shows an image. (Genuine
    # agent screenshots follow an assistant turn, so they're kept.)
    deduped = []
    for t in turns:
        if (t.pop("_tool_echo", False)
                and deduped and deduped[-1]["role"] == "user" and deduped[-1]["images"]):
            continue
        t.pop("_tool_echo", None)
        deduped.append(t)
    # Mid-turn messages are DEAD-END nodes: Claude Code writes the queued user
    # message as a child of the in-flight assistant record, but the reply chain
    # continues through the tool_result SIBLING — so the leaf→root walk never
    # visits them and chat silently hid every message sent while working (the
    # stuck-QUEUED-visual bug). Merge them back in: a user record that branches
    # OFF the active chain (parent on-branch, itself unvisited) is a message
    # Phil really sent. The parent-in-seen guard keeps pre-compaction/old-branch
    # history out.
    extras = []

    def _add_extra(uuid, clean, images, ts, idx):
        # ESC-drained duplicates: the same text re-sent as a REAL turn just
        # after the dead-end node → show only the real one. Same guard between
        # extras (a message can leave both a dead-end node and an attachment).
        s = clean.strip()
        if any(t["role"] == "user" and (t["text"] or "").strip() == s
               and abs(order.get(t["id"], idx) - idx) < 400
               for t in deduped + extras):
            return
        order[uuid] = idx
        extras.append({"id": uuid, "role": "user", "text": clean,
                       "images": images, "ts": ts})

    for i, e in enumerate(lines):
        if (e.get("type") == "user" and e.get("uuid")
                and e["uuid"] not in seen
                and not e.get("isSidechain")
                and e.get("parentUuid") in seen):
            text = _msg_text(e)
            if not text or _is_harness_noise(text):
                continue
            clean, images = _extract_images(_strip_system_blocks(text))
            if clean or images:
                _add_extra(e["uuid"], clean, images, e.get("timestamp"), i)
        # Shape 2: some mid-turn sends never get a user node at all — just an
        # `attachment` record with the prompt (queue-op enqueue → remove).
        elif e.get("type") == "attachment" and e.get("uuid"):
            att = e.get("attachment") or {}
            if (att.get("type") == "queued_command"
                    and (att.get("origin") or {}).get("kind") == "human"):
                prompt = (att.get("prompt") or "").strip()
                if prompt and not _is_harness_noise(prompt):
                    clean, images = _extract_images(prompt)
                    if clean or images:
                        _add_extra(e["uuid"], clean, images,
                                   att.get("timestamp") or e.get("timestamp"), i)
    if extras:
        deduped = sorted(deduped + extras, key=lambda t: order.get(t["id"], 0))
    return deduped


# When a turn ends, the Stop hook records the timestamp here. A live session is
# "working" only if its transcript changed AFTER the last turn ended — this is
# robust across long quiet gaps (thinking, slow tool calls) that a simple
# recency window would misread as idle.
_last_stop = {}  # session_id -> epoch seconds of last Stop/Notification


def _entry_ts(e):
    from datetime import datetime
    t = e.get("timestamp") or ""
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _last_turn_state(path, sid: str):
    """Working state from the ACTUAL conversation, not the file mtime. Metadata
    writes (ai-title, file-history-snapshot, mode, queue-operation, attachment)
    bump mtime without any real work and caused phantom 'working' after a resume.
    Returns True (generating / awaiting a tool), False (last turn finished), or
    None (can't tell → caller falls back to the mtime heuristic)."""
    try:
        lines = _read_lines(path)
    except Exception:
        return None
    for e in reversed(lines):
        if e.get("type") not in ("user", "assistant"):
            continue  # skip ai-title / mode / snapshot / queue-operation / attachment
        msg = e.get("message") or {}
        role = msg.get("role")
        if role == "assistant":
            sr = msg.get("stop_reason")
            if sr in ("end_turn", "stop_sequence"):
                return False   # Claude finished the turn → idle
            if sr == "tool_use":
                return True    # called a tool, awaiting result → working
            # Unknown/partial stop_reason: the desktop app persists the final
            # assistant line before stamping stop_reason. If the Stop hook fired
            # at/after this message, the turn is done → idle; else it's streaming.
            ls = _last_stop.get(sid)
            ts = _entry_ts(e)
            if ls is not None and ts is not None and ts <= ls + 2:
                return False
            return None        # mid-stream / unknown → let caller decide
        if role == "user":
            # A trailing user/tool message means awaiting Claude — UNLESS a Stop
            # was recorded after it (stale queued/leftover entry → idle).
            ls = _last_stop.get(sid)
            ts = _entry_ts(e)
            if ls is not None and ts is not None and ts < ls:
                return False
            return True
    return None


# ---- Live background-subagent tracking -------------------------------------------
# Claude Code (v2.1.x) fires SubagentStart/SubagentStop hooks when a background agent
# spawns / finishes. We keep a per-session set of live subagent ids so is_working()
# reports True while a background agent runs even though the MAIN terminal has already
# returned to an idle prompt (the "← for agents" state, which the spinner scan misses).
# Entries auto-expire so a dropped Stop hook can't wedge a session "working" forever.
# COUNTER, not an id-set: Start = +1, Stop = -1. The old id-keyed set wedged the
# session "working" when a SubagentStop's agent_id didn't match its Start's (observed
# in the log: a Stop that left the count unchanged → stuck spinner until the 30-min TTL).
# Counting can't mismatch: N starts + N stops always nets to 0. `ts` = last change, only
# a backstop for a genuinely-dropped Stop.
_subagents = {}            # sid -> {"count": int, "ts": last_change_ts}
_SUBAGENT_TTL = 240        # 4 min backstop for a MISSED Stop (was 30 min — too long; a stuck
                           # subagent spun the side menu for half an hour). Most subagents finish
                           # in <2 min; a genuinely longer one re-reads working from the terminal
                           # anyway once it prints. Phil hates false-WORKING more than a rare early clear.


def _subagent_running(sid: str) -> bool:
    e = _subagents.get(sid)
    if not e:
        return False
    if time.time() - e["ts"] > _SUBAGENT_TTL:
        _subagents.pop(sid, None)    # dropped Stop / crashed subagent → self-heal
        return False
    return e["count"] > 0


def is_working(sid: str, live: bool, mtime: float, job_running: bool, path=None,
               terminal_snapshot: bool = False) -> bool:
    # TERMINAL IS THE BRAIN. If this session runs as a live EZ terminal, its own
    # status line is the ONE truth: "esc to interrupt" is on screen iff Claude is
    # generating / running a tool. Read that and stop — never let a transcript
    # heuristic say "working" when the terminal has already stopped (that exact
    # divergence is what broke trust: STOP quiets the terminal but chat kept
    # spinning). Cache-only on the hot path; a background thread keeps it warm.
    ez = ez_name_for(sid)
    if gc_ez.is_alive(ez):
        w = gc_ez.is_working(ez, allow_snapshot=terminal_snapshot, force=terminal_snapshot)
        # Terminal is the ONLY truth for a live EZ session. Return it verbatim —
        # NEVER fall through to the mtime/transcript heuristics below, which
        # manufacture phantom "working" (a metadata write bumps mtime → busy=True)
        # that the app can't tell from real work. A cold cache (w is None) reads as
        # idle here; the 0.6s warmer fills it in and the next poll corrects it. This
        # is what keeps the side dot, chat banner, and terminal banner in lockstep.
        # OR a background subagent is in flight (main prompt idle but real work running).
        return bool(w) or _subagent_running(sid)
    # A Ground-Control-OWNED session knows its state authoritatively: busy is set
    # the instant a message hits stdin and cleared on the matching stream-json
    # `result` — the SAME truth the terminal spinner reports, from the same stream.
    _b = _sessions.busy(sid)
    if _b is not None:
        return _b
    if job_running:
        return True
    if not live:
        return False
    # NO live terminal and NOT GC-owned → we have no TRUTHFUL real-time signal here.
    # The terminal is the boss. Transcript-parsing and mtime windows only ever
    # MANUFACTURE phantom "working": a metadata write (auto-title, snapshot) bumps
    # mtime, or a transcript whose tail is a `tool_use` reads "open" forever after
    # the turn is long done. That inference is exactly what spun FA: Marketing / FA:
    # Script on the side menu, and the invariants ban it. So an unowned, terminal-less
    # session is IDLE unless a background subagent is genuinely in flight (the one
    # truthful non-terminal signal — the SubagentStart/Stop hook counter). Phil hates
    # a false "working" far more than a rare missed one.
    # Long-term fix: born-in-EZ so every session has a real terminal pulse (WORKSPACE NEXT).
    return _subagent_running(sid)


def _work_progress(path: Path):
    """Elapsed seconds + output tokens since the last real user message."""
    from datetime import datetime

    lines = _read_lines(path)
    idx = None
    for i in range(len(lines) - 1, -1, -1):
        e = lines[i]
        if e.get("type") == "user" and not e.get("isSidechain") and e.get("uuid") and _msg_text(e):
            idx = i
            break
    if idx is None:
        return None
    ts = lines[idx].get("timestamp") or ""
    try:
        start = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    tokens, seen_mid = 0, set()
    for e in lines[idx:]:
        if e.get("type") == "assistant":
            m = e.get("message") or {}
            mid = m.get("id")
            if mid and mid in seen_mid:
                continue
            if mid:
                seen_mid.add(mid)
            tokens += (m.get("usage") or {}).get("output_tokens") or 0
    return {"seconds": max(0, int(time.time() - start)), "tokens": tokens}


def session_meta(path: Path):
    """Cheap metadata for the list view."""
    lines = _read_lines(path)
    title, preview, cwd = None, None, None
    for e in lines:
        if cwd is None and e.get("cwd"):
            cwd = e["cwd"]
        if title is None and e.get("type") == "user" and not e.get("isSidechain"):
            title = _msg_text(e)
        if title and cwd:
            break
    for e in reversed(lines):
        if _is_turn(e):
            t = _msg_text(e)
            if t:
                preview = t
                break
    return title, preview, cwd


# ---------------------------------------------------------------- liveness

def live_sessions():
    """sessionId -> pid for sessions with a running desktop/CLI process."""
    out = {}
    for f in glob.glob(str(SESSIONS_DIR / "*.json")):
        try:
            d = json.load(open(f))
            pid, sid = d.get("pid"), d.get("sessionId")
            if pid and sid:
                try:
                    os.kill(pid, 0)
                    out[sid] = pid
                except OSError:
                    pass
        except (json.JSONDecodeError, OSError):
            pass
    return out


def release_session(session_id: str, timeout: float = 6.0) -> bool:
    """Kill the live process holding a session, wait for it to die."""
    pid = live_sessions().get(session_id)
    if not pid:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.25)
        except OSError:
            return True
    return False


# ---------------------------------------------------------------- jobs

_jobs = {}  # session_id -> {status, started, result, error}
_jobs_lock = threading.Lock()

# unread alert tracking: which sessions fired an alert Phil hasn't viewed yet
UNREADS_PATH = Path(__file__).parent / "unreads.json"
_unreads_lock = threading.Lock()


def _load_unreads():
    if UNREADS_PATH.exists():
        try:
            return json.load(open(UNREADS_PATH))
        except json.JSONDecodeError:
            pass
    return {}


def mark_unread(session_id: str):
    with _unreads_lock:
        u = _load_unreads()
        u[session_id] = time.time()
        json.dump(u, open(UNREADS_PATH, "w"))
        return len(u)


_called = set()   # sids we've already phone-called for the CURRENT unread episode

# Cross-device dismiss sync (iMessage-style): when a session is acknowledged on ANY device,
# record it here so the OTHER devices can pull that session's leftover notification banner.
_dismissed = []   # [{sid, ts}] recent acknowledgments (newest last, capped)
_dismissed_lock = threading.Lock()


def _record_dismiss(session_id: str):
    with _dismissed_lock:
        _dismissed.append({"sid": session_id, "ts": time.time()})
        if len(_dismissed) > 300:
            del _dismissed[:-300]


def clear_unread(session_id: str, reason: str = "?"):
    had = False
    with _unreads_lock:
        u = _load_unreads()
        if session_id in u:
            had = True
            del u[session_id]
            json.dump(u, open(UNREADS_PATH, "w"))
    if had:
        print(f"[unread] {time.strftime('%H:%M:%S')} cleared {session_id[:8]} (reason={reason})", flush=True)
        _record_dismiss(session_id)   # tell the other device to pull this session's banner
    _called.discard(session_id)   # acknowledged → allow a future call next episode


def reconcile_unreads(visible_ids: set):
    """Drop unread entries whose session isn't in the visible list. A badge you
    can't tap to clear is worse than no badge — this keeps the icon count equal
    to sessions the user can actually open."""
    with _unreads_lock:
        u = _load_unreads()
        stale = [k for k in u if k not in visible_ids]
        if stale:
            for k in stale:
                u.pop(k, None)
            json.dump(u, open(UNREADS_PATH, "w"))
            print(f"[unread] {time.strftime('%H:%M:%S')} reconcile dropped {[k[:8] for k in stale]} (not in {len(visible_ids)} visible)", flush=True)
            for k in stale:
                _called.discard(k)


_procs = {}  # session_id -> Popen (for interrupt)


def _run_injection(session_id: str, cwd: str, text: str, resume: bool):
    cmd = [CLAUDE_BIN, "-p"]
    if resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]
    cmd += [text, "--output-format", "json", "--permission-mode", "bypassPermissions"]
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        _procs[session_id] = proc
        try:
            out, err = proc.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        _procs.pop(session_id, None)
        if proc.returncode is not None and proc.returncode < 0:
            with _jobs_lock:
                _jobs[session_id] = {"status": "stopped", "finished": time.time()}
            return
        result, error = None, None
        try:
            d = json.loads((out or "").strip().splitlines()[-1])
            result = d.get("result")
            if d.get("is_error"):
                error = result or "unknown error"
        except (json.JSONDecodeError, IndexError):
            error = (err or out or "no output")[-500:]
        with _jobs_lock:
            _jobs[session_id] = {
                "status": "error" if error else "done",
                "result": result,
                "error": error,
                "finished": time.time(),
            }
    except Exception as e:  # noqa: BLE001
        _procs.pop(session_id, None)
        with _jobs_lock:
            _jobs[session_id] = {"status": "error", "error": str(e), "finished": time.time()}


def _flush_restored_input(name: str):
    """After an ESC interrupt, submit any message Claude Code restored into the
    terminal's input line (queued mid-turn sends come back UNSENT). Detection is
    strict: a line starting with ❯ that has text after it, sitting directly
    under a horizontal rule (the composer box top). Dialog/menu ❯ pointers are
    inside │-bordered boxes and never match."""
    for _ in range(2):  # the interrupt redraw can lag; check twice
        time.sleep(0.9)
        try:
            lines = gc_ez._render_lines(name) or []
        except Exception:
            return
        for i, ln in enumerate(lines):
            st = ln.strip()
            if st.startswith("❯") and st[1:].strip():
                if i > 0 and lines[i - 1].lstrip().startswith("───"):
                    gc_ez.send_input(name, "\r")
                    return


@app.post("/api/session/{project_dir}/{session_id}/stop")
def stop_session(project_dir: str, session_id: str):
    # EZ terminal session (the "brain") → interrupt the live turn by sending ESC
    # straight into the PTY, exactly like pressing Escape in the terminal. NEVER
    # kill the process and NEVER stamp _last_stop: the terminal is the source of
    # truth for busy, so let it report idle once the interrupt actually lands.
    # (Stamping _last_stop here made the app read "idle" while Claude kept working
    # in the terminal — the disappearing-working-bar desync.)
    ez = ez_name_for(session_id)
    if gc_ez.is_alive(ez):
        gc_ez.send_input(ez, "\x1b")
        # Claude Code restores queued (mid-turn) messages into the composer
        # UNSENT on interrupt — in a terminal you'd press Enter yourself, but a
        # chat-initiated ESC would strand them there forever (the stuck-QUEUED
        # bug). Follow up: once the interrupt redraw lands, if text is sitting
        # in the input line (❯ …, directly under the box's rule line — menus'
        # ❯ pointers never are), press Enter for the user. An extra Enter on an
        # empty composer is a no-op, so the worst case is harmless.
        threading.Thread(target=_flush_restored_input, args=(ez,), daemon=True).start()
        return {"ok": True, "stopped": True, "what": "terminal esc"}
    # Owned session → interrupt the current turn without killing the process.
    if _sessions.is_owned_live(session_id):
        _sessions.stop(session_id)
        return {"ok": True, "stopped": True, "what": "owned turn"}
    # Phone-initiated run → terminate it.
    proc = _procs.get(session_id)
    if proc and proc.poll() is None:
        proc.terminate()
        with _jobs_lock:
            _jobs[session_id] = {"status": "stopped", "finished": time.time()}
        return {"ok": True, "stopped": True, "what": "phone job"}
    # Also drop anything queued.
    with _queue_lock:
        _queues.pop(session_id, None)
    # Desktop-run turn → kill the live process (desktop app respawns the
    # session from disk when reopened; transcript is safe).
    pid = live_sessions().get(session_id)
    if pid:
        release_session(session_id)
        _last_stop[session_id] = time.time()
        return {"ok": True, "stopped": True, "what": "desktop turn"}
    return {"ok": True, "stopped": False}


# ---------------------------------------------------------------- api

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- iMessage assistant
# The always-on iMessage agent (separate LaunchAgent, has Full Disk Access) reads
# ~/.imessage-agent/config.json every loop and writes actions.jsonl. The GC app flips
# the toggle by writing config here, and watches the feed by reading the log. The
# server itself needs no special permission — it only touches these two plain files.
_IMSG_DIR = Path.home() / ".imessage-agent"
_IMSG_CONFIG = _IMSG_DIR / "config.json"
_IMSG_LOG = _IMSG_DIR / "actions.jsonl"


def _imsg_running() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        return "com.philipbuonforte.imessage-agent" in out
    except Exception:
        return False


def _imsg_config() -> dict:
    try:
        c = json.load(open(_IMSG_CONFIG))
    except Exception:
        c = {}
    threads = c.get("threads") or {}
    if not isinstance(threads, dict):
        threads = {}
    # per-thread override ∈ {"default","off","draft","send"}; "default" (or absent)
    # follows the global enabled/dry_run. Keep only valid values.
    threads = {k: v for k, v in threads.items() if v in ("off", "draft", "send")}
    scopes = c.get("scopes") or {}
    if not isinstance(scopes, dict):
        scopes = {}
    # scope = list of project categories the router may reference for this thread
    scopes = {k: [g for g in (v or []) if isinstance(g, str)]
              for k, v in scopes.items() if v}
    rules = c.get("rules") or {}   # per-thread free-text instructions
    if not isinstance(rules, dict):
        rules = {}
    rules = {k: v for k, v in rules.items() if isinstance(v, str) and v.strip()}
    return {"enabled": bool(c.get("enabled", False)),
            "dry_run": bool(c.get("dry_run", True)),
            "threads": threads, "scopes": scopes, "rules": rules}


def _imsg_effective_mode(sender: str, cfg: dict) -> str:
    """Resolve a thread to off/draft/send. OPT-IN model: every thread is OFF by
    default; the assistant only works a thread you explicitly turned on (draft or
    send). The master switch is a global kill — OFF → everything off."""
    if not cfg["enabled"]:
        return "off"
    ov = (cfg.get("threads") or {}).get(sender)
    return ov if ov in ("draft", "send") else "off"


# ── contact / thread name resolution ─────────────────────────────────────────
# Show the SAME names the user sees in Messages: contact names for 1:1 chats
# (from macOS Contacts) and group display names (from chat.db). Built once and
# cached — contacts/group names change rarely.
_AB_GLOB = str(Path.home() / "Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")
_CHATDB = Path.home() / "Library/Messages/chat.db"
_name_cache = {"ts": 0.0, "phones": {}, "emails": {}, "groups": {}}
_NAME_TTL = 600


def _digits10(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


def _build_name_maps():
    # A number can map to MULTIPLE contact cards (e.g. a wife saved as both
    # "Tara Buonforte" and "Mom" by the kids). Keep the BEST candidate, matching
    # how Messages shows it: prefer a full first+last name over a one-word
    # nickname/relationship label ("Mom", "Dad"), then a business name.
    phones, emails, groups = {}, {}, {}          # key -> name
    pscore, escore = {}, {}                       # key -> score of stored name

    def scored(fn, ln, org):
        fn, ln, org = (fn or "").strip(), (ln or "").strip(), (org or "").strip()
        if fn and ln:
            return (" ".join([fn, ln]), 3)        # full personal name — best
        if fn or ln:
            return (fn or ln, 2)                   # a personal name beats a business label
        if org:
            return (org, 1)                        # business-only (e.g. a shortcode)
        return ("", 0)

    def offer(store, scores, key, fn, ln, org):
        nm, sc = scored(fn, ln, org)
        if nm and sc > scores.get(key, 0):
            store[key] = nm
            scores[key] = sc

    for db in glob.glob(_AB_GLOB):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for fn, ln, org, num in c.execute(
                "SELECT r.ZFIRSTNAME,r.ZLASTNAME,r.ZORGANIZATION,p.ZFULLNUMBER "
                "FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON p.ZOWNER=r.Z_PK "
                "WHERE p.ZFULLNUMBER IS NOT NULL"):
                offer(phones, pscore, _digits10(num), fn, ln, org)
            for fn, ln, org, addr in c.execute(
                "SELECT r.ZFIRSTNAME,r.ZLASTNAME,r.ZORGANIZATION,e.ZADDRESS "
                "FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER=r.Z_PK "
                "WHERE e.ZADDRESS IS NOT NULL"):
                offer(emails, escore, (addr or "").lower().strip(), fn, ln, org)
            c.close()
        except Exception:
            pass
    try:
        c = sqlite3.connect(f"file:{_CHATDB}?mode=ro", uri=True)
        for cid, dn in c.execute(
            "SELECT chat_identifier, display_name FROM chat "
            "WHERE display_name IS NOT NULL AND display_name != ''"):
            groups[cid] = dn
        c.close()
    except Exception:
        pass
    return phones, emails, groups


def _name_maps():
    if time.time() - _name_cache["ts"] > _NAME_TTL:
        p, e, g = _build_name_maps()
        if p or e or g:
            _name_cache.update({"ts": time.time(), "phones": p, "emails": e, "groups": g})
        else:
            _name_cache["ts"] = time.time()   # avoid hammering on failure
    return _name_cache


def _contact_name(handle: str):
    h = (handle or "").strip()
    m = _name_maps()
    if "@" in h:
        return m["emails"].get(h.lower())
    return m["phones"].get(_digits10(h))


def _pretty_phone(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:]}"
    return s or "?"


def _thread_key(sender: str, chat: str) -> str:
    """Stable per-conversation id: the group's chat_identifier for group chats,
    else the individual handle (so old sender-keyed rows still line up)."""
    if chat and ";+;" in chat:
        return chat.split(";+;")[-1]
    return sender or "?"


def _display_title(sender: str, chat: str) -> str:
    """The label to show — mirrors Messages: group name, contact name, or a
    nicely formatted phone number as the last resort."""
    if chat and ";+;" in chat:
        cid = chat.split(";+;")[-1]
        return _name_maps()["groups"].get(cid) or "Group chat"
    return _contact_name(sender) or _pretty_phone(sender)


# ── real Messages threads (chat.db) ──────────────────────────────────────────
# So the Text Assistant inbox mirrors the actual Messages app — ALL recent
# threads, named the same — not just the handful the agent has acted on.
_MSG_SKIP = {'streamtyped', 'NSAttributedString', 'NSObject', 'NSString',
             'NSDictionary', 'NSNumber', 'NSValue', 'iI', 'NSMutableAttributedString',
             'NSAttributeInfo', 'NSMutableString'}


def _msg_decode(text, blob) -> str:
    if text:
        return text
    if not blob:
        return ""
    runs = [r.decode(errors="ignore") for r in re.findall(rb'[ -~]{2,}', blob)]

    def ok(r):
        if r in _MSG_SKIP or r.startswith('__k'):
            return False
        if 'kIM' in r or 'Attribute' in r or 'GUID' in r or r.startswith('NS'):
            return False
        if 'at_0_' in r or re.search(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}', r):
            return False   # attachment filename / UUID, not message text
        return not re.fullmatch(r'[\x00-\x2f]*', r or '')

    cand = [r for r in runs if ok(r)]
    if not cand:
        return ""
    best = max(cand, key=len)
    # typedstream length/type bytes often leak a 1-2 char junk prefix ("+%", "+F")
    if best[:1] in "+" and len(best) > 2:
        best = best[2:]
    return best.strip()


def _apple_ts(ns) -> str:
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ns / 1e9 + 978307200).isoformat(timespec="seconds")
    except Exception:
        return ""


def _group_participants_title(conn, cid: str) -> str:
    try:
        rows = conn.execute(
            "SELECT h.id FROM chat c JOIN chat_handle_join chj ON chj.chat_id=c.ROWID "
            "JOIN handle h ON h.ROWID=chj.handle_id WHERE c.chat_identifier=?", (cid,)).fetchall()
        names = []
        for (hid,) in rows:
            nm = _contact_name(hid) or _pretty_phone(hid)
            first = nm.split()[0] if nm else hid
            if first not in names:
                names.append(first)
        if not names:
            return ""
        if len(names) <= 3:
            return ", ".join(names)
        return ", ".join(names[:3]) + f" & {len(names) - 3} more"
    except Exception:
        return ""


_chats_cache = {"ts": 0.0, "data": []}
_CHATS_TTL = 6


def _recent_chats(limit: int = 40):
    """Recent Messages threads (last message per chat), newest first."""
    if time.time() - _chats_cache["ts"] < _CHATS_TTL and _chats_cache["data"]:
        return _chats_cache["data"]
    out = []
    try:
        c = sqlite3.connect(f"file:{_CHATDB}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT c.chat_identifier, c.display_name, c.style, m.text, m.attributedBody, "
            "       m.is_from_me, m.date "
            "FROM chat c "
            "JOIN chat_message_join j ON j.chat_id = c.ROWID "
            "JOIN message m ON m.ROWID = j.message_id "
            "JOIN (SELECT j2.chat_id cid, MAX(m2.date) md "
            "      FROM chat_message_join j2 JOIN message m2 ON m2.ROWID=j2.message_id "
            "      GROUP BY j2.chat_id) last ON last.cid = c.ROWID AND last.md = m.date "
            "ORDER BY m.date DESC LIMIT ?", (limit,))
        for cid, dn, style, text, ab, isme, date in rows:
            is_group = (style == 43) or (cid or "").startswith("chat")
            title = (dn or "").strip()
            if not title:
                title = (_group_participants_title(c, cid) or "Group chat") if is_group \
                    else (_contact_name(cid) or _pretty_phone(cid))
            txt = _msg_decode(text, ab)
            if not txt:
                txt = "📷 Attachment"
            elif isme:
                txt = "You: " + txt
            out.append({"cid": cid, "title": title, "is_group": is_group,
                        "last_text": txt, "ts": _apple_ts(date)})
        c.close()
    except Exception:
        pass
    if out:
        _chats_cache.update({"ts": time.time(), "data": out})
    return out


def _chat_messages(cid: str, limit: int = 40):
    """Recent real messages in one thread → bubbles for the thread view."""
    out = []
    try:
        c = sqlite3.connect(f"file:{_CHATDB}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT m.text, m.attributedBody, m.is_from_me, m.date, h.id "
            "FROM chat c "
            "JOIN chat_message_join j ON j.chat_id = c.ROWID "
            "JOIN message m ON m.ROWID = j.message_id "
            "LEFT JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE c.chat_identifier=? ORDER BY m.date DESC LIMIT ?", (cid, limit)).fetchall()
        for text, ab, isme, date, hid in reversed(rows):
            body = _msg_decode(text, ab)
            if not body:
                continue
            out.append({"is_from_me": bool(isme), "text": body,
                        "ts": _apple_ts(date), "handle": hid})
        c.close()
    except Exception:
        pass
    return out


@app.get("/api/imessage/status")
def imessage_status():
    cfg = _imsg_config()
    installed = _IMSG_DIR.exists()
    # quick counts from the log tail
    replied = seen = 0
    try:
        lines = _IMSG_LOG.read_text().splitlines()[-500:]
        for ln in lines:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("event") == "decision":
                seen += 1
                if e.get("decision", {}).get("action") == "reply":
                    replied += 1
    except Exception:
        pass
    return {"installed": installed, "running": _imsg_running(),
            "enabled": cfg["enabled"], "dry_run": cfg["dry_run"],
            "seen": seen, "replied": replied}


class IMsgConfig(BaseModel):
    # Optional[...] not `bool | None` — the X | Y union syntax needs Python
    # 3.10+ and is EVALUATED at class creation, so it crashes the whole server
    # at import on the 3.9 that Apple ships (Hank's lockout, 2026-07-25).
    # Installed copies run on whatever python3 the user's Mac has.
    enabled: Optional[bool] = None
    dry_run: Optional[bool] = None


@app.post("/api/imessage/config")
def imessage_set_config(body: IMsgConfig):
    cfg = _imsg_config()
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.dry_run is not None:
        cfg["dry_run"] = body.dry_run
    _IMSG_DIR.mkdir(exist_ok=True)
    json.dump(cfg, open(_IMSG_CONFIG, "w"))
    return {"ok": True, **cfg}


class IMsgThreadMode(BaseModel):
    mode: str   # "default" | "off" | "draft" | "send"


@app.post("/api/imessage/thread/{sender}/mode")
def imessage_set_thread_mode(sender: str, body: IMsgThreadMode):
    """Per-conversation control. OPT-IN model: 'off'/'default' turn the thread off
    (the default — clears any override); 'draft'/'send' turn this thread ON."""
    cfg = _imsg_config()
    threads = cfg.get("threads") or {}
    m = (body.mode or "off").lower()
    if m in ("off", "default"):
        threads.pop(sender, None)   # off IS the default → no stored override
        m = "off"
    elif m in ("draft", "send"):
        threads[sender] = m
    else:
        return {"ok": False, "error": "bad mode"}
    cfg["threads"] = threads
    _IMSG_DIR.mkdir(exist_ok=True)
    json.dump(cfg, open(_IMSG_CONFIG, "w"))
    return {"ok": True, "sender": sender,
            "mode": m, "effective": _imsg_effective_mode(sender, cfg)}


def _project_categories() -> list:
    """Distinct project categories (the desktop groups, minus the live 'Active'
    overlay) — the buckets a thread's routing can be limited to."""
    try:
        d = list_sessions()
    except Exception:
        return []
    out = []
    for g in d.get("groups", []):
        n = g.get("name")
        if n and n != "Active" and n not in out and g.get("sessions"):
            out.append(n)
    return out


@app.get("/api/imessage/project-groups")
def imessage_project_groups():
    return {"groups": _project_categories()}


_IMSG_POLICIES = _IMSG_DIR / "policies.json"


def _load_policies() -> dict:
    try:
        return json.load(open(_IMSG_POLICIES))
    except Exception:
        return {}


@app.get("/api/imessage/projects")
def imessage_projects():
    """Every project (session), deduped, with its category + current sharing rules —
    powers the 'what can each session share' editor."""
    try:
        d = list_sessions()
    except Exception:
        d = {"groups": []}
    pols = _load_policies()
    by_cwd = {}
    for g in d.get("groups", []):
        gname = g.get("name")
        for s in g.get("sessions", []):
            cwd = s.get("cwd")
            if not cwd:
                continue
            rec = by_cwd.get(cwd)
            if rec is None:
                rec = by_cwd[cwd] = {"title": s.get("title") or s.get("project"),
                                     "cwd": cwd, "category": None,
                                     "policy": (pols.get(cwd) or "")}
            if gname and gname != "Active" and rec["category"] is None:
                rec["category"] = gname
    return {"projects": sorted(by_cwd.values(),
                               key=lambda x: (x["category"] or "zz", x["title"] or ""))}


class IMsgPolicy(BaseModel):
    cwd: str
    text: str = ""


@app.post("/api/imessage/policy")
def imessage_set_policy(body: IMsgPolicy):
    """Set the sharing rules for a project (by folder). Empty clears them."""
    pols = _load_policies()
    if (body.text or "").strip():
        pols[body.cwd] = body.text.strip()
    else:
        pols.pop(body.cwd, None)
    _IMSG_DIR.mkdir(exist_ok=True)
    json.dump(pols, open(_IMSG_POLICIES, "w"))
    return {"ok": True, "cwd": body.cwd, "policy": pols.get(body.cwd, "")}


class IMsgRules(BaseModel):
    text: str = ""


@app.post("/api/imessage/thread/{sender}/rules")
def imessage_set_thread_rules(sender: str, body: IMsgRules):
    """Free-text rules for THIS conversation (e.g. 'never discuss Link-X with Tara').
    Empty clears them."""
    cfg = _imsg_config()
    rules = cfg.get("rules") or {}
    t = (body.text or "").strip()
    if t:
        rules[sender] = t
    else:
        rules.pop(sender, None)
    cfg["rules"] = rules
    _IMSG_DIR.mkdir(exist_ok=True)
    json.dump(cfg, open(_IMSG_CONFIG, "w"))
    return {"ok": True, "sender": sender, "rules": rules.get(sender, "")}


class IMsgScope(BaseModel):
    groups: list = []   # allowed categories; empty = any


@app.post("/api/imessage/thread/{sender}/scope")
def imessage_set_thread_scope(sender: str, body: IMsgScope):
    """Limit which project categories the router may reference for THIS thread.
    Empty list = any project (the default)."""
    cfg = _imsg_config()
    scopes = cfg.get("scopes") or {}
    raw = body.groups or []
    if "__none__" in raw:
        groups = ["__none__"]      # explicitly NO projects (personal/general only)
    else:
        valid = set(_project_categories())
        groups = [g for g in raw if g in valid]
    if groups:
        scopes[sender] = groups
    else:
        scopes.pop(sender, None)   # empty = any → no stored restriction
    cfg["scopes"] = scopes
    _IMSG_DIR.mkdir(exist_ok=True)
    json.dump(cfg, open(_IMSG_CONFIG, "w"))
    return {"ok": True, "sender": sender, "scope": groups}


_IMSG_PENDING = _IMSG_DIR / "pending.json"
_IMSG_SEND = Path.home() / ".claude/skills/send-text/send_imessage.sh"


def _load_pending() -> list:
    try:
        return json.load(open(_IMSG_PENDING))
    except Exception:
        return []


def _save_pending(items):
    json.dump(items, open(_IMSG_PENDING, "w"))


@app.get("/api/imessage/pending")
def imessage_pending():
    """Drafts waiting for your approval (draft mode). Newest first."""
    return {"items": list(reversed(_load_pending()))}


@app.post("/api/imessage/pending/{pid}/send")
def imessage_pending_send(pid: str):
    items = _load_pending()
    draft = next((d for d in items if d.get("id") == pid), None)
    if not draft:
        return JSONResponse({"error": "draft not found"}, status_code=404)
    # Send via the same script the agent uses (Messages control + Full Disk Access).
    reply = draft["reply"]
    if not reply.startswith("Agent:"):
        reply = "Agent: " + reply   # recipients always know it's the assistant
    try:
        r = subprocess.run([str(_IMSG_SEND), "--chat", draft["chat"], reply],
                           capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
    except Exception as e:
        return JSONResponse({"error": f"send failed: {e}"}, status_code=502)
    _save_pending([d for d in items if d.get("id") != pid])
    # record it in the activity log as sent-by-you
    try:
        with open(_IMSG_LOG, "a") as f:
            f.write(json.dumps({"event": "action", "action": "reply", "sent": True,
                                "approved": True, "sender": draft["sender"],
                                "would_send": reply,
                                "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds")}) + "\n")
    except Exception:
        pass
    # If this draft promised an action, queue it so the agent routes the work now
    # that you've actually sent the message.
    fu = (draft.get("followup") or "").strip()
    if ok and fu:
        try:
            with open(_IMSG_DIR / "followup_queue.jsonl", "a") as f:
                f.write(json.dumps({"task": fu, "sender": draft.get("sender"),
                                    "chat": draft.get("chat")}) + "\n")
        except Exception:
            pass
    return {"ok": ok}


class IMsgEscalate(BaseModel):
    sender: str
    text: str = ""
    call: Optional[bool] = None   # also phone-call Phil


@app.post("/api/imessage/escalate")
def imessage_escalate(body: IMsgEscalate):
    """The texts-watcher calls this when someone asks for Phil (or it shouldn't
    answer). Pushes to Phil's phone and — if asked — places a Bland call."""
    who = _display_title(body.sender, None)
    push_title = f"📱 {who} is asking for you"
    push_body = (body.text or "")[:160]
    try:
        send_apns(push_title, push_body)
    except Exception:
        pass
    called = False
    if body.call:
        num = _settings().get("call_number", "")
        if num:
            try:
                called = _place_call(
                    num, f"Hey, this is your text assistant. {who} is texting you and "
                         f"asked to reach you. They said: {body.text[:200]}. "
                         f"Open Ground Control when you can.")
            except Exception:
                called = False
    return {"ok": True, "pushed": True, "called": called}


@app.post("/api/imessage/pending/{pid}/dismiss")
def imessage_pending_dismiss(pid: str):
    items = _load_pending()
    _save_pending([d for d in items if d.get("id") != pid])
    return {"ok": True}


def _pending_by_key() -> dict:
    """thread_key -> list of pending drafts."""
    out: dict = {}
    for d in _load_pending():
        k = _thread_key(d.get("sender", "?"), d.get("chat"))
        out.setdefault(k, []).append(d)
    return out


@app.get("/api/imessage/conversations")
def imessage_conversations():
    """The Text Assistant inbox — mirrors the Messages app: ALL recent threads,
    named the same (contact names, group names). The assistant's drafts / mode
    overlay onto them. A synthetic 'Commands' thread carries your GC commands."""
    cfg = _imsg_config()
    pend = _pending_by_key()
    convos = []
    seen = set()

    def add(key, title, preview, ts, is_group):
        seen.add(key)
        dc = len(pend.get(key, []))
        convos.append({
            "sender": key, "title": title,
            "lastTs": ts, "preview": (preview or "")[:80],
            "lastType": "draft" if dc else None,
            "draftCount": dc, "isGroup": is_group,
            "mode": (cfg.get("threads") or {}).get(key, "off"),
            "effectiveMode": _imsg_effective_mode(key, cfg),
            "scope": (cfg.get("scopes") or {}).get(key, []),
            "rules": (cfg.get("rules") or {}).get(key, ""),
        })

    # 1) real Messages threads
    for ch in _recent_chats(40):
        add(ch["cid"], ch["title"], ch["last_text"], ch["ts"], ch["is_group"])

    # 2) any thread with a pending draft that wasn't in the recent list (older) —
    #    never hide something waiting on you.
    for key, drafts in pend.items():
        if key in seen:
            continue
        d = max(drafts, key=lambda x: x.get("ts") or "")
        title = _display_title(d.get("sender", key), d.get("chat"))
        add(key, title, d.get("reply"), d.get("ts"), bool(d.get("chat") and ";+;" in d.get("chat")))

    # 3) the synthetic Commands thread (from the agent log)
    cmd_last, cmd_any = None, False
    try:
        for ln in _IMSG_LOG.read_text().splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("event") == "command" and e.get("status") != "running":
                cmd_any = True
                cmd_last = e
    except Exception:
        pass
    if cmd_any and cmd_last:
        convos.append({
            "sender": "__commands__", "title": "Commands",
            "lastTs": cmd_last.get("ts"),
            "preview": (cmd_last.get("command") or "")[:80],
            "lastType": "command", "draftCount": 0, "isGroup": False,
            "mode": "default", "effectiveMode": None,
        })

    convos.sort(key=lambda c: c.get("lastTs") or "", reverse=True)
    return {"conversations": convos}


@app.get("/api/imessage/thread/{key}/messages")
def imessage_thread_messages(key: str):
    """Full thread for the reader: the real back-and-forth from Messages, with the
    assistant's ignored-notes / sent-markers / pending drafts overlaid."""
    key = urllib.parse.unquote(key)
    if key == "__commands__":
        items = []
        try:
            for ln in _IMSG_LOG.read_text().splitlines():
                try:
                    e = json.loads(ln)
                except Exception:
                    continue
                if e.get("event") == "command" and e.get("status") != "running":
                    items.append({"type": "command", "ts": e.get("ts"),
                                  "text": e.get("command"), "result": e.get("result"),
                                  "failed": e.get("status") == "error"})
        except Exception:
            pass
        return {"items": items, "title": "Commands"}

    # agent overlays, indexed by the message text they refer to
    ignored, sent_texts, notes = {}, set(), []
    try:
        for ln in _IMSG_LOG.read_text().splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if _thread_key(e.get("sender", "?"), e.get("chat")) != key:
                continue
            ev = e.get("event")
            if ev == "decision":
                d = e.get("decision", {})
                if d.get("action") == "none" and e.get("in"):
                    ignored[e.get("in")] = d.get("reason")
            elif ev == "action" and e.get("action") == "reply" and e.get("sent"):
                t = e.get("would_send") or e.get("decision", {}).get("text")
                if t:
                    sent_texts.add(t)
            elif ev == "dispatch":
                st = e.get("status")
                if st == "dispatched":
                    txt = f"⚙️ Handling this in “{e.get('session', 'a session')}” — {e.get('task', '')}"
                elif st == "no-session":
                    txt = f"⚙️ No matching session for: {e.get('task', '')}"
                else:
                    txt = f"⚙️ {e.get('task', '')}"
                notes.append({"type": "note", "ts": e.get("ts"), "text": txt})
            elif ev == "escalate":
                notes.append({"type": "note", "ts": e.get("ts"),
                              "text": "🙋 Asked for you — texted" + (" & called you" if e.get("called") else " you")})
    except Exception:
        pass

    items = list(notes)
    for m in _chat_messages(key, 40):
        body = m["text"]
        if m["is_from_me"]:
            # assistant-sent vs Phil-sent
            if body in sent_texts:
                items.append({"type": "sent", "ts": m["ts"], "text": body})
            else:
                items.append({"type": "me", "ts": m["ts"], "text": body})
        else:
            it = {"type": "incoming", "ts": m["ts"], "text": body}
            if body in ignored:
                it["ignored"] = True
                it["reason"] = ignored[body]
            items.append(it)

    # pending drafts (not yet in chat.db) at the end
    for d in sorted(pend_for(key), key=lambda x: x.get("ts") or ""):
        items.append({"type": "draft", "ts": d.get("ts"), "text": d.get("reply"),
                      "reason": d.get("reason"), "draftId": d.get("id")})

    items.sort(key=lambda x: x.get("ts") or "")
    ch_title = next((c["title"] for c in _recent_chats(60) if c["cid"] == key), None)
    return {"items": items, "title": ch_title or _display_title(key, None)}


def pend_for(key: str):
    return _pending_by_key().get(key, [])


@app.get("/api/imessage/activity")
def imessage_activity(limit: int = 50):
    """Human-facing feed: recent texts the agent saw + what it decided."""
    out = []
    try:
        lines = _IMSG_LOG.read_text().splitlines()
    except Exception:
        return {"items": []}
    for ln in reversed(lines):
        if len(out) >= max(1, min(limit, 200)):
            break
        try:
            e = json.loads(ln)
        except Exception:
            continue
        ev = e.get("event")
        if ev == "decision":
            d = e.get("decision", {})
            # Only "ignored" decisions belong in the history feed. A reply-decision is
            # either sitting in the pending queue (its own section) or will appear as a
            # "sent" action — showing it here too would double it / mislabel it.
            if d.get("action") == "none":
                out.append({"kind": "decision", "ts": e.get("ts"),
                            "sender": e.get("sender"), "text": e.get("in"),
                            "action": "none", "reason": d.get("reason")})
        elif ev == "action" and e.get("action") == "reply" and e.get("sent"):
            out.append({"kind": "sent", "ts": e.get("ts"),
                        "sender": e.get("sender"),
                        "reply": e.get("would_send") or e.get("decision", {}).get("text")})
        elif ev == "command":
            # Only surface a command once (its "done"/"error" line has the result);
            # skip the interim "running" line to avoid a duplicate row.
            if e.get("status") != "running":
                out.append({"kind": "command", "ts": e.get("ts"),
                            "text": e.get("command"), "reason": e.get("result"),
                            "action": e.get("status")})
        elif ev in ("toggle", "killswitch"):
            out.append({"kind": ev, "ts": e.get("ts"),
                        "enabled": e.get("enabled"), "state": e.get("state")})
    return {"items": out}


# ---------------------------------------------------------------- usage analytics

# Rough weight of each model against your plan's usage allowance (Opus is the
# heavy hitter; Sonnet/Haiku are far lighter). Used only for a relative
# "allowance-weighted" view, not real billing.
_MODEL_WEIGHT = {"opus": 5.0, "sonnet": 1.0, "haiku": 0.25, "fable": 5.0}
_usage_cache = {}  # path -> {size, offset, events:[...]}
_usage_lock = threading.Lock()


def _model_family(m: str) -> str:
    m = (m or "").lower()
    for k in ("opus", "sonnet", "haiku", "fable"):
        if k in m:
            return k
    return "other"


def _scan_usage_file(path: Path, title: str, project: str):
    """Incrementally extract per-message token usage from one transcript."""
    from datetime import datetime

    st = path.stat()
    with _usage_lock:
        c = _usage_cache.get(str(path))
        if c and c["size"] == st.st_size:
            return c["events"]
        if c is None or st.st_size < c["size"]:
            c = {"offset": 0, "events": []}
    events = list(c["events"])
    with open(path, "rb") as f:
        f.seek(c["offset"])
        chunk = f.read()
    for raw in chunk.split(b"\n"):
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        ts = d.get("timestamp") or ""
        try:
            epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        fam = _model_family(msg.get("model", ""))
        inp = u.get("input_tokens", 0)
        out = u.get("output_tokens", 0)
        cr = u.get("cache_read_input_tokens", 0)
        cw = u.get("cache_creation_input_tokens", 0)
        total = inp + out + cr + cw
        events.append({
            "ts": epoch, "model": fam, "session": title, "project": project,
            "in": inp, "out": out, "cr": cr, "cw": cw, "total": total,
            "weighted": total * _MODEL_WEIGHT.get(fam, 1.0),
        })
    with _usage_lock:
        _usage_cache[str(path)] = {"size": st.st_size, "offset": c["offset"] + len(chunk), "events": events}
    return events


@app.get("/api/usage")
def usage(days: int = 30):
    cutoff = time.time() - days * 86400
    recs = _desktop_records()
    idx = _transcript_index()
    events = []
    for r in recs.values():
        sid = r.get("cliSessionId")
        path = idx.get(sid)
        if not path:
            continue
        title = (r.get("title") or "Untitled")[:60]
        project = Path(r.get("cwd") or "").name
        try:
            for e in _scan_usage_file(path, title, project):
                if e["ts"] >= cutoff:
                    events.append(e)
        except OSError:
            continue
    events.sort(key=lambda e: e["ts"])
    return {"events": events, "now": time.time(),
            "weights": _MODEL_WEIGHT}


def _event_cost(e: dict) -> float:
    """Real $ for one usage event, from _CTX_PRICES[family] = (in, out, cr, cw) per 1M."""
    p = _CTX_PRICES.get(e.get("model"), _CTX_PRICES["opus"])
    return (e["in"] * p[0] + e["out"] * p[1] + e["cr"] * p[2] + e["cw"] * p[3]) / 1_000_000.0


@app.get("/api/activity")
def activity(start: float = 0.0, end: float = 0.0):
    """What's running now + real per-session token/time/$ usage in a [start,end] window.
    Powers the Usage → Activity tab. Both bounds are epoch seconds; end<=0 means "now".
    Covers EVERY session on this Mac (bridge-driven, other-project, Silver Lands, etc.) —
    NOT just app-born ones — by scanning the full transcript index + all live EZ terminals.
    active_min = distinct 1-minute buckets that emitted an assistant message (real work
    time, not wall-clock the tab was left open)."""
    now = time.time()
    if end <= 0:
        end = now
    idx = _transcript_index()          # sid -> transcript path, ALL projects
    ezmap = _load_ez_names()           # sid -> clean EZ handle ("LX Website")
    recs_by_sid = {}                   # sid -> desktop record (for the nicest title/cwd)
    for r in _desktop_records().values():
        s = r.get("cliSessionId")
        if s:
            recs_by_sid[s] = r

    def _label(sid: str) -> tuple:
        """(title, project). Title prefers a real name; for bridge/other sessions the
        first user message is /requests spam, so fall back to the PROJECT name, never
        that. cwd is resolved once here."""
        r = recs_by_sid.get(sid)
        cwd = r.get("cwd") if r else None
        if not cwd:
            p = idx.get(sid)
            if p:
                _, _, cwd = session_meta(p)
                if not cwd:  # decode the encoded project-dir as a last resort
                    cwd = p.parent.name.replace("-", "/")
        project = Path(cwd).name if cwd else ""
        if r and r.get("title"):
            title = r["title"][:60]
        else:
            nm = ezmap.get(sid)
            title = (nm[:60] if (nm and not _UUIDISH.match(nm)) else (project or sid[:8]))
        return title, project

    # ---- Active now: EVERY live EZ terminal that's genuinely working ----
    active = []
    for name in gc_ez.list_sessions():
        if not working_by_ez(name):
            continue
        sid = sid_for_ez(name) or name
        path = idx.get(sid)
        prog = _work_progress(path) if path else None
        title, project = _label(sid)
        active.append({
            "id": sid,
            "title": name if not _UUIDISH.match(name) else title,
            "project": project,
            "elapsedSec": (prog or {}).get("seconds", 0),
            "turnTokens": (prog or {}).get("tokens", 0),
        })
    active.sort(key=lambda a: -a["elapsedSec"])

    # ---- Usage in window: aggregate real transcript events across ALL sessions ----
    def _cheap_title(sid: str) -> str:
        r = recs_by_sid.get(sid)
        if r and r.get("title"):
            return r["title"][:60]
        nm = ezmap.get(sid)
        return nm[:60] if (nm and not _UUIDISH.match(nm)) else ""

    def _cheap_proj(sid: str) -> str:
        r = recs_by_sid.get(sid)
        return Path(r["cwd"]).name if (r and r.get("cwd")) else ""

    agg = {}
    for sid, path in idx.items():
        try:
            evs = _scan_usage_file(path, _cheap_title(sid), _cheap_proj(sid))
        except OSError:
            continue
        for e in evs:
            if e["ts"] < start or e["ts"] > end:
                continue
            a = agg.get(sid)
            if a is None:
                a = agg[sid] = {"id": sid, "model": e["model"],
                                "in": 0, "out": 0, "cr": 0, "cw": 0,
                                "total": 0, "cost": 0.0, "msgs": 0,
                                "firstTs": e["ts"], "lastTs": e["ts"], "_min": set()}
            a["in"] += e["in"]; a["out"] += e["out"]; a["cr"] += e["cr"]; a["cw"] += e["cw"]
            a["total"] += e["total"]; a["cost"] += _event_cost(e); a["msgs"] += 1
            a["firstTs"] = min(a["firstTs"], e["ts"]); a["lastTs"] = max(a["lastTs"], e["ts"])
            a["_min"].add(int(e["ts"] // 60))
            a["model"] = e["model"]  # last model seen in window

    sessions = []
    for a in agg.values():
        a["title"], a["project"] = _label(a["id"])
        a["activeMin"] = len(a.pop("_min"))
        a["cost"] = round(a["cost"], 4)
        # A model switch we made via /model is newer than anything in the window until
        # the next reply lands — without this the Usage row kept showing the OLD model
        # (same lag the chat label had, different surface).
        with _model_override_lock:
            ov = _MODEL_OVERRIDE.get(a["id"])
        if ov and ov[1] > a.get("lastTs", 0):
            a["model"] = _model_family(ov[0])
        # Context comes from THIS transcript, not from a join against /api/context-all.
        # That endpoint only covers app-OWNED sessions, while this one covers every
        # transcript on disk — so any session started outside the app (Silver Lands)
        # showed usage with a blank context. Computing it here means it's always there.
        p = idx.get(a["id"])
        if p is not None:
            try:
                info = _ctx_for_path(p)
                a["ctxPct"] = info.get("pct", 0)
                a["ctxCost"] = info.get("cost", 0)
            except Exception:  # noqa: BLE001
                pass
        sessions.append(a)
    sessions.sort(key=lambda s: -s["total"])

    totals = {
        "tokens": sum(s["total"] for s in sessions),
        "cost": round(sum(s["cost"] for s in sessions), 4),
        "activeMin": sum(s["activeMin"] for s in sessions),
        "sessions": len(sessions),
    }
    return {"now": now, "range": {"start": start, "end": end},
            "active": active, "sessions": sessions, "totals": totals}


# ---- Custom icons (group covers + per-session avatars) --------------------
# The server is the source of truth so icons SYNC across devices: either app
# uploads here, every app polls /api/icons (key → mtime) and refetches changes.
ICONS_DIR = Path(__file__).resolve().parent / "icons"
ICONS_DIR.mkdir(exist_ok=True)
_ICON_KEY_RE = _re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


ROBOTS_DIR = Path(__file__).resolve().parent / "static" / "robots"


@app.get("/api/robots")
def robots_index():
    """Names of the built-in robot avatars (sliced from Phil's sprite sheet).
    The apps' robot picker renders this catalog; picking one copies it to the
    session's icon slot via the normal /api/icon upload path."""
    return sorted(p.stem for p in ROBOTS_DIR.glob("robot_*.png"))


@app.get("/api/robot/{name}")
def robot_get(name: str):
    if not _re.match(r"^robot_\d{2}$", name):
        return JSONResponse({"error": "bad name"}, status_code=400)
    p = ROBOTS_DIR / f"{name}.png"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/icons")
def icons_index():
    """Key → mtime for every stored icon. Apps diff this against what they hold."""
    return {p.stem: p.stat().st_mtime for p in ICONS_DIR.glob("*.img")}


@app.get("/api/icon/{key}")
def icon_get(key: str):
    if not _ICON_KEY_RE.match(key):
        return JSONResponse({"error": "bad key"}, status_code=400)
    p = ICONS_DIR / f"{key}.img"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/jpeg")


@app.post("/api/icon/{key}")
async def icon_set(key: str, request: Request):
    if not _ICON_KEY_RE.match(key):
        return JSONResponse({"error": "bad key"}, status_code=400)
    data = await request.body()
    if not data or len(data) > 10_000_000:
        return JSONResponse({"error": "bad image"}, status_code=400)
    p = ICONS_DIR / f"{key}.img"
    p.write_bytes(data)
    return {"ok": True, "mtime": p.stat().st_mtime}


@app.delete("/api/icon/{key}")
def icon_delete(key: str):
    if not _ICON_KEY_RE.match(key):
        return JSONResponse({"error": "bad key"}, status_code=400)
    (ICONS_DIR / f"{key}.img").unlink(missing_ok=True)
    return {"ok": True}


_balance_cache = {"ts": 0.0, "val": None}
# Last-good plan limits. Anthropic's OAuth usage API rate-limits hard (429) when the
# app polls every few seconds, and a raw {'error':'api_429'} is undecodable client-side
# → the Usage sheet's top meters blank. So we cache the last GOOD response and serve it
# through a 429/timeout, and only actually hit Anthropic once per TTL.
_limits_cache = {"ts": 0.0, "val": None, "last_try": 0.0, "err": None}
_LIMITS_TTL = 60    # serve a good snapshot this long without touching Anthropic
_LIMITS_RETRY = 20  # while we have NO good value, attempt the API at most this often


def _decrypt_chrome_cookies(db_path: str, host_like: str = "%claude.ai%") -> dict:
    """Decrypt the Claude desktop app's (Chromium/Electron) cookie jar for a host.
    macOS: AES-128-CBC, key = PBKDF2-SHA1(<'Claude Safe Storage' keychain pw>,
    'saltysalt', 1003), IV = 16 spaces. Returns {name: value}."""
    import sqlite3, hashlib, shutil, tempfile
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pw = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Claude Safe Storage", "-w"],
        text=True, timeout=5,
    ).strip()
    key = hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, 16)
    tmp = tempfile.mktemp(suffix=".db")  # copy: the live DB may be WAL-locked
    shutil.copy(db_path, tmp)
    try:
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
            (host_like,),
        ).fetchall()
        con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    jar = {}
    for name, enc in rows:
        if not enc or enc[:3] != b"v10":
            continue
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        raw = dec.update(enc[3:]) + dec.finalize()
        raw = raw[: -raw[-1]]  # strip PKCS7 padding
        try:
            v = raw.decode("utf-8")
        except UnicodeDecodeError:
            v = raw[32:].decode("utf-8", "ignore")  # some builds prepend a domain hash
        jar[name] = v
    return jar


def _fetch_credit_balance():
    """Live prepaid usage-credit balance ($), read from the Claude desktop app's
    own claude.ai session on THIS Mac. The OAuth usage API returns null for the
    balance; only claude.ai's cookie-authed billing endpoint
    (/organizations/{org}/prepaid/credits) exposes it. Cached 5m; keeps the last
    good value if a read fails."""
    if time.time() - _balance_cache["ts"] < 300:
        return _balance_cache["val"]
    val = _balance_cache["val"]
    try:
        db = os.path.expanduser("~/Library/Application Support/Claude/Cookies")
        jar = _decrypt_chrome_cookies(db)
        sk, org = jar.get("sessionKey"), jar.get("lastActiveOrg")
        if sk and org:
            import httpx

            cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
            ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Claude/1.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36")
            r = httpx.get(
                f"https://claude.ai/api/organizations/{org}/prepaid/credits",
                headers={"Cookie": cookie, "User-Agent": ua, "Accept": "*/*",
                         "anthropic-client-platform": "web_claude_ai",
                         "Referer": "https://claude.ai/"},
                timeout=12,
            )
            if r.status_code == 200:
                amt = r.json().get("amount")
                if amt is not None:
                    val = amt / 100.0  # minor units → dollars
    except Exception:  # noqa: BLE001 — cookie/session may be absent; keep last good
        pass
    _balance_cache["ts"] = time.time()
    _balance_cache["val"] = val
    return val


def _fetch_limits_raw():
    """Live plan limits from this Mac's Claude Code OAuth token. Returns the raw
    Anthropic dict, or {'error': ...}.

    Cached for _LIMITS_TTL seconds and keeps the last GOOD value: a fresh cache hit
    returns instantly (one real API call per TTL, not one per poll), and a transient
    429/timeout serves the last good value instead of an undecodable error — which is
    what blanked the Usage sheet's top meters. When we have no good value yet, the API
    is retried at most once per _LIMITS_RETRY so a sustained 429 can't be hammered into
    never recovering (each poll re-hitting Anthropic just re-arms the rate limit)."""
    now = time.time()
    val = _limits_cache["val"]
    # Fresh good snapshot → serve it, no network.
    if val is not None and now - _limits_cache["ts"] < _LIMITS_TTL:
        return val
    # Back off: don't touch Anthropic more than once per retry window.
    if now - _limits_cache["last_try"] < _LIMITS_RETRY:
        return val or _limits_cache["err"] or {"error": "api_429"}
    _limits_cache["last_try"] = now
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            text=True, timeout=5,
        ).strip()
        tok = json.loads(raw)["claudeAiOauth"]["accessToken"]
    except Exception:  # noqa: BLE001
        return val or {"error": "no_token"}
    import httpx

    try:
        r = httpx.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"},
            timeout=15,
        )
        if r.status_code != 200:
            # Never hand the app an undecodable {'error':...} when we have a good snapshot.
            _limits_cache["err"] = {"error": f"api_{r.status_code}"}
            return val or _limits_cache["err"]
        d = r.json()
        _limits_cache["ts"] = now
        _limits_cache["val"] = d
        return d
    except Exception as e:  # noqa: BLE001
        _limits_cache["err"] = {"error": str(e)}
        return val or _limits_cache["err"]


@app.get("/api/limits")
def limits():
    """Real, live plan limits — the exact numbers from Settings → Usage."""
    return JSONResponse(_fetch_limits_raw(), status_code=200)


@app.get("/api/usage-summary")
def usage_summary():
    """Flattened plan usage for the app's /usage command — the app doesn't have to
    parse the raw Anthropic blob."""
    d = _fetch_limits_raw()
    if "error" in d:
        return {"error": d["error"]}
    five = d.get("five_hour", {}) or {}
    week = d.get("seven_day", {}) or {}
    extra = d.get("extra_usage", {}) or {}
    scoped = []
    for l in d.get("limits", []):
        if l.get("kind") == "weekly_scoped":
            nm = ((l.get("scope") or {}).get("model") or {}).get("display_name") or "model"
            scoped.append({"name": nm, "pct": l.get("percent", 0),
                           "critical": l.get("severity") == "critical"})
    credits = (extra.get("used_credits") or 0) / (10 ** (extra.get("decimal_places") or 0))

    # Credit dollars: spent + cap + remaining, when the account exposes them.
    # Uncapped pay-as-you-go accounts report null limit/balance → remaining stays
    # null and the app shows "spent · no cap". If a cap/balance appears upstream,
    # remaining fills in automatically.
    def _dollars(obj):
        if isinstance(obj, dict):
            return (obj.get("amount_minor") or 0) / (10 ** (obj.get("exponent") or 0))
        if isinstance(obj, (int, float)):
            return float(obj)
        return None
    spend = d.get("spend", {}) or {}
    limit_dollars = _dollars(spend.get("limit"))
    if limit_dollars is None and extra.get("monthly_limit") is not None:
        limit_dollars = _dollars(extra.get("monthly_limit"))
    balance_dollars = _dollars(spend.get("balance"))
    remaining = None
    if balance_dollars is not None:
        remaining = balance_dollars
    elif limit_dollars is not None:
        remaining = max(0.0, limit_dollars - credits)

    return {
        "five_pct": float(five.get("utilization") or 0),
        "five_resets": five.get("resets_at") or "",
        "week_pct": float(week.get("utilization") or 0),
        "week_resets": week.get("resets_at") or "",
        "scoped": scoped,
        "credits_used": credits,
        "credits_limit": limit_dollars,
        "credits_remaining": remaining,
        "credits_balance": _fetch_credit_balance(),  # live prepaid $ balance
        "can_purchase_credits": bool(spend.get("can_purchase_credits")),
        "overage_on": bool(extra.get("is_enabled")),
    }


def _ctx_for_path(path: Path) -> dict:
    """Context-window stats for one transcript (last assistant usage). Shared by
    /api/context and the all-sessions context list."""
    usage, model = None, ""
    for e in reversed(_read_lines(path)):
        msg = e.get("message") or {}
        if msg.get("role") == "assistant" and msg.get("usage"):
            usage, model = msg["usage"], msg.get("model", "")
            break
    if not usage:
        return {"context_tokens": 0, "window": 200000, "pct": 0, "cost": 0}
    inp = usage.get("input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    ctx = inp + cr + cw
    window = 1000000 if ctx > 200000 else 200000
    pi, po, pcr, pcw = _CTX_PRICES[_model_family(model)]
    cost = inp * pi / 1e6 + cr * pcr / 1e6 + cw * pcw / 1e6 + out * po / 1e6
    return {"context_tokens": ctx, "window": window,
            "pct": round(ctx / window * 100, 1), "cost": round(cost, 4),
            "model": _model_family(model)}


@app.get("/api/context-all")
def context_all(limit: int = 30):
    """Per-session context fullness — powers the 'Context by session' list in the
    app's Usage view.

    ONE SOURCE RULE: this list must show EXACTLY the sessions the sidebar shows,
    with the sidebar's own titles. It used to enumerate every transcript on disk
    (archived sessions, desktop one-offs, dead twins) under a different title
    registry — so the Usage sheet showed 'Untitled' rows, stale names ('Ground
    Control' vs the renamed 'Ground Control Master'), and duplicates that don't
    exist in the app. Same eligibility as /api/sessions: OWNED by the app, not
    archived, transcript on disk; title from the same desktop record."""
    recs = _desktop_records()
    idx = _transcript_index()
    owned = _load_owned()
    by_sid = {}   # dedupe: a sid can have twin desktop records; the newest title wins
    for r in recs.values():
        sid = r.get("cliSessionId")
        if not sid or sid not in owned or r.get("isArchived"):
            continue
        path = idx.get(sid)
        if path is None or not path.exists():
            continue
        by_sid[sid] = (r, path)
    rows = sorted(by_sid.items(), key=lambda kv: kv[1][1].stat().st_mtime,
                  reverse=True)[:max(1, min(limit, 100))]
    out = []
    for sid, (r, path) in rows:
        try:
            info = _ctx_for_path(path)
        except Exception:  # noqa: BLE001
            continue
        if info.get("context_tokens", 0) <= 0:
            continue
        out.append({"id": sid, "dir": path.parent.name,
                    "title": (r.get("title") or "Untitled")[:60],
                    "project": Path(r.get("cwd") or "").name, **info})
    out.sort(key=lambda x: x.get("pct", 0), reverse=True)
    return out


_MAC_DOWNLOAD_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ground Control for Mac</title>
<style>
 :root{--clay:#D97757;--paper:#FAF9F5;--ink:#2b2622}
 *{box-sizing:border-box} body{margin:0;background:var(--paper);color:var(--ink);
   font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
   display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
 .card{max-width:460px;width:100%;background:#fff;border-radius:20px;padding:36px;
   box-shadow:0 12px 40px rgba(0,0,0,.08);text-align:center}
 .logo{width:76px;height:76px;border-radius:18px;background:var(--clay);margin:0 auto 18px;
   display:flex;align-items:center;justify-content:center;font-size:40px;color:#fff}
 h1{font-size:24px;margin:0 0 6px} p{color:#6b625b;margin:0 0 22px}
 a.btn{display:inline-block;background:var(--clay);color:#fff;text-decoration:none;
   font-weight:700;padding:14px 30px;border-radius:12px;font-size:17px}
 ol{text-align:left;color:#4a423c;font-size:14.5px;margin:26px 0 0;padding-left:20px}
 ol li{margin:8px 0} code{background:#f0ece6;padding:1px 6px;border-radius:5px;font-size:13px}
</style></head><body><div class=card>
 <div class=logo>✳</div>
 <h1>Ground Control for Mac</h1>
 <p>The native desktop app. It talks to the server already running on this Mac.</p>
 <a class=btn href="/download/mac.zip" download>Download for Mac</a>
 <ol>
  <li>Unzip the download (it may unzip automatically).</li>
  <li>Drag <b>Ground Control</b> into your <b>Applications</b> folder.</li>
  <li>First open: <b>right-click the app → Open → Open</b> (this app isn't from the
      App Store, so macOS asks once).</li>
  <li>It connects to <code>http://localhost:8130</code> automatically. Done.</li>
 </ol>
</div></body></html>"""


@app.get("/download/mac")
def download_mac_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_MAC_DOWNLOAD_PAGE)


@app.get("/api/mac-version")
def mac_version():
    """What Mac build this server hands out. Installed copies poll this and compare it
    to their own CFBundleVersion — that's how a Mac user learns an update exists
    (TestFlight tells iPhone users; nothing told Mac users before this)."""
    p = STATIC_DIR / "download" / "mac-version.json"
    try:
        return json.load(open(p))
    except (OSError, json.JSONDecodeError):
        return {"build": 0, "version": "", "notes": "", "published": 0}


@app.get("/download/mac.zip")
def download_mac_zip():
    return FileResponse(STATIC_DIR / "download" / "GroundControl-mac.zip",
                        media_type="application/zip",
                        filename="Ground Control.zip")


@app.get("/usage")
def usage_page():
    return FileResponse(STATIC_DIR / "usage.html")


@app.get("/groundzero")
def groundzero_page():
    return FileResponse(STATIC_DIR / "groundzero.html")


@app.get("/alert-flow")
def alert_flow_page():
    return FileResponse(STATIC_DIR / "alert-flow.html")


@app.get("/how-it-works")
def how_it_works_page():
    """Plain-English explainer of the whole architecture (brain / windows / tunnel).
    Linked from Settings on both apps, and shareable to anyone."""
    return FileResponse(STATIC_DIR / "how-it-works.html")


@app.post("/api/ack/{sid}")
def ack_session(sid: str):
    """Explicitly acknowledge a session's alert — clears unread so repeats + the phone
    call stop, WITHOUT navigating away and back. Lets the app 'acknowledge in place'
    (click the session you're already on, or an Acknowledge button)."""
    clear_unread(sid, reason="ack-button")
    return {"ok": True}


# ============================================================================
# TERMINAL AS THE BRAIN — EZ (raw-PTY) sessions + xterm bridge
# ============================================================================

def working_by_ez(name: str) -> bool:
    """THE one working-state computation, keyed by EZ handle. Every surface — the
    side-menu dot (list_sessions), the chat/terminal banner (/api/work), Ground Zero
    — MUST resolve through this so they can NEVER disagree. A live-EZ session with a
    dead handle reads idle; the canonical sid comes from the same reverse lookup, so
    there is exactly one branch, one sid, one answer.

    (This killed the class of bug where the dot re-derived busy via build_session's
    is_working() with legacy `job_running`/mtime args while /api/work took the clean
    EZ path — same session, two answers, phantom spinner.)"""
    if not name or not gc_ez.is_alive(name):
        return False
    sid = sid_for_ez(name) or name
    return is_working(sid, True, 0.0, False, None)


@app.get("/api/work/{name}")
def work_state(name: str):
    """Single source of truth for the working status of an EZ terminal, so the chat
    banner, the terminal banner, and the side dot all read the SAME thing. Returns
    {working, label} — label is Claude's own status line ('Brewed · 1 shell still
    running') or null."""
    # Read the WARM CACHE (not a per-poll forced snapshot). Force-reading here snapshotted
    # the viewed session's EZ socket every second, contending with the live terminal WS →
    # laggy typing on the phone. The cache is kept correct by the 0.6s warmer.
    working = working_by_ez(name)
    _, label = gc_ez.work_status(name)
    if working and not label:
        label = "Background agent…"
    return {"working": working, "label": label}


@app.get("/terminal")
def terminal_page():
    # NEVER cache the terminal page — the WKWebView was serving a stale copy
    # (heuristic caching) so fixes to the page's JS didn't reach the phone. The
    # page is tiny; always fetch fresh so a redeploy takes effect on next open.
    return FileResponse(STATIC_DIR / "terminal.html",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                 "Pragma": "no-cache", "Expires": "0"})


@app.get("/xterm.min.js")
def _xterm_js():
    return FileResponse(STATIC_DIR / "xterm.min.js", media_type="application/javascript")


@app.get("/xterm.min.css")
def _xterm_css():
    return FileResponse(STATIC_DIR / "xterm.min.css", media_type="text/css")


@app.get("/xterm-addon-fit.min.js")
def _xterm_fit():
    return FileResponse(STATIC_DIR / "xterm-addon-fit.min.js", media_type="application/javascript")


@app.get("/xterm-addon-webgl.min.js")
def _xterm_webgl():
    return FileResponse(STATIC_DIR / "xterm-addon-webgl.min.js", media_type="application/javascript")


@app.get("/xterm-addon-canvas.min.js")
def _xterm_canvas():
    return FileResponse(STATIC_DIR / "xterm-addon-canvas.min.js", media_type="application/javascript")


class EzNewBody(BaseModel):
    cwd: str
    text: str = ""


# --- EZ name registry: decouple the human EZ handle from the Claude session id ---
# Historically the EZ socket name == the Claude session id (a uuid). We now let a
# session carry a friendly EZ handle ("ground control") that is the real terminal
# name, while the Claude side keeps its resume id. This maps {claude_sid: ez_name}.
_EZ_NAMES_PATH = Path(__file__).parent / "ez_names.json"
_ez_names_lock = threading.Lock()


def _load_ez_names() -> dict:
    try:
        return json.load(open(_EZ_NAMES_PATH))
    except (OSError, json.JSONDecodeError):
        return {}


def _set_ez_name(sid: str, ez: str) -> None:
    with _ez_names_lock:
        d = _load_ez_names()
        d[sid] = ez
        json.dump(d, open(_EZ_NAMES_PATH, "w"), indent=2)


def ez_name_for(sid: str) -> str:
    """EZ socket/handle name for a Claude session id. Falls back to the sid itself
    — pre-naming sessions were launched with socket name == sid, so old sessions
    stay attachable with no migration."""
    return _load_ez_names().get(sid, sid)


_UUIDISH = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.I)


def _name_flag(name: str) -> list:
    """`--name <name>` args for a claude launch — but only when `name` is a real
    human handle, not a bare UUID (old sessions use the sid as the handle). Keeps
    the /resume picker + terminal title matching `ez ls` / the app for named
    sessions, without stamping a UUID as the display name on legacy ones."""
    return [] if (not name or _UUIDISH.match(name)) else ["--name", name]


def sid_for_ez(ez: str):
    """Reverse lookup: which Claude session id does this EZ handle drive?"""
    for sid, name in _load_ez_names().items():
        if name == ez:
            return sid
    return None


# --- /clear rotation tracking -------------------------------------------------
# `/clear` in the terminal starts a NEW conversation id + transcript file while the
# same claude process keeps running. GC's stable identity stays the ORIGINAL sid;
# without tracking, chat kept reading the pre-clear transcript and refresh/wake
# resumed the OLD id — resurrecting the conversation Phil had just cleared.
# The successor transcript's first record re-declares the session's custom title
# (we launch every session `--name`d), which is the thread we follow.
_ROT_CACHE = {}  # sid -> {"ts": float, "eff": str}
_STABLE_CACHE = {}  # rotated sid -> stable sid


def _stable_sid(sid: str, transcript: str = "") -> str:
    """Map a /clear-rotated conversation id back to GC's STABLE session id.

    Hooks fire with the live process's CURRENT sid; after /clear that's a rotated id
    GC has no record of — which made the alert gate treat an engaged session as
    'untouched/autonomous' and silently mute it. The rotated transcript's first
    records declare the session's custom title; the GC session whose EZ name matches
    is the stable identity. Unknown/unmatched sids pass through unchanged."""
    names = _load_ez_names()
    if sid in names:
        return sid                      # already a stable, GC-known id
    if sid in _STABLE_CACHE:
        return _STABLE_CACHE[sid]
    stable = sid
    try:
        path = Path(transcript) if transcript else None
        if path is None or not path.exists():
            hits = list(PROJECTS_DIR.glob(f"*/{sid}.jsonl"))
            path = hits[0] if hits else None
        if path is not None and path.exists():
            with open(path) as fh:
                for _ in range(3):
                    line = fh.readline()
                    if not line:
                        break
                    d = json.loads(line)
                    if d.get("type") == "custom-title":
                        title = d.get("customTitle")
                        for s, n in names.items():
                            if n == title:
                                stable = s
                        break
    except (OSError, json.JSONDecodeError):
        pass
    _STABLE_CACHE[sid] = stable
    return stable


def _effective_sid(project_dir: str, sid: str) -> str:
    """The sid of this session's CURRENT conversation — follows /clear rotations.

    The identity family = the original transcript plus every transcript whose first
    records declare this session's custom title (each /clear rotation re-declares
    it). The LIVE conversation is whichever family member was written to most
    recently — terminal is truth: exactly one claude process owns this session, and
    it appends to its current file as it works. Falls back to the original sid for
    unnamed/legacy sessions and on any doubt."""
    name = ez_name_for(sid)
    if not name or _UUIDISH.match(name):
        return sid          # unnamed session → no custom-title thread to follow
    c = _ROT_CACHE.get(sid)
    if c and time.time() - c["ts"] < 5:
        return c["eff"]
    eff = sid
    try:
        dirp = PROJECTS_DIR / project_dir
        orig = dirp / f"{sid}.jsonl"
        best_m = orig.stat().st_mtime if orig.exists() else 0.0
        for f in dirp.glob("*.jsonl"):
            if f.name.startswith("agent-") or f.stem == sid:
                continue
            m = f.stat().st_mtime
            if m <= best_m:
                continue
            # family members re-declare the custom title in their first records
            try:
                with open(f) as fh:
                    for _ in range(3):
                        line = fh.readline()
                        if not line:
                            break
                        d = json.loads(line)
                        if d.get("type") == "custom-title":
                            if d.get("customTitle") == name:
                                eff, best_m = f.stem, m
                            break
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    _ROT_CACHE[sid] = {"ts": time.time(), "eff": eff}
    return eff


# --- App-owned sessions: the app only shows sessions BORN in it (through EZ), not
# the whole Claude-desktop world. A session outside the app can't be truly shared
# (only --resume'd into a divergent twin), so mirroring them is a false promise.
_OWNED_PATH = Path(__file__).parent / "gc_owned.json"
_owned_lock = threading.Lock()


def _load_owned() -> set:
    try:
        return set(json.load(open(_OWNED_PATH)))
    except (OSError, json.JSONDecodeError):
        return set()


def _add_owned(sid: str) -> None:
    with _owned_lock:
        d = _load_owned()
        d.add(sid)
        json.dump(sorted(d), open(_OWNED_PATH, "w"), indent=2)


_claude_json_lock = threading.Lock()


def _pretrust_folder(cwd: str) -> None:
    """Pre-accept claude's per-folder gates for `cwd` in ~/.claude.json so a new
    session boots straight to the input prompt. The "Do you trust this folder?" and
    "allow external CLAUDE.md imports?" prompts are NOT skipped by bypassPermissions
    and would otherwise swallow the first message. Deterministic — no TUI parsing."""
    cfg_path = Path.home() / ".claude.json"
    try:
        with _claude_json_lock:
            cfg = json.load(open(cfg_path))
            p = cfg.setdefault("projects", {}).setdefault(cwd, {})
            p["hasTrustDialogAccepted"] = True
            p["hasClaudeMdExternalIncludesApproved"] = True
            p["hasClaudeMdExternalIncludesWarningShown"] = True
            p.setdefault("allowedTools", [])
            p.setdefault("hasCompletedProjectOnboarding", True)
            tmp = str(cfg_path) + ".gctmp"
            json.dump(cfg, open(tmp, "w"))
            os.replace(tmp, cfg_path)
    except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
        print(f"[pretrust] {e}", flush=True)


@app.post("/api/ez/new")
def ez_new(body: EzNewBody):
    """Launch a brand-new session as `claude` inside an EZ PTY."""
    import uuid as _uuid
    cwd = str(Path(body.cwd).expanduser())
    if not os.path.isdir(cwd):
        return JSONResponse({"error": "folder does not exist"}, status_code=400)
    sid = str(_uuid.uuid4())
    _forge_desktop_record(sid, cwd, (body.text or "New session")[:60])
    gc_ez.start(sid, cwd, [CLAUDE_BIN, "--session-id", sid,
                           "--permission-mode", "bypassPermissions"])
    enc = cwd.replace("/", "-").replace(" ", "-").replace(".", "-").replace("_", "-")
    return {"ok": True, "name": sid, "dir": enc, "alive": gc_ez.is_alive(sid)}


@app.post("/api/ez/{name}/ensure")
def ez_ensure(name: str):
    """Make sure an EZ terminal is running for this session — resume it if not.
    This is how any session becomes the live 'terminal brain' on demand."""
    sid = sid_for_ez(name) or name
    # Terminal is the single brain. If an owned-stdin process is ALSO resuming
    # this session (leftover from the chat/owned path), evict it now — two
    # `claude --resume <sid>` processes on one transcript diverge, which is the
    # terminal/chat "not in sync" bug.
    if _sessions.is_owned_live(sid):
        _sessions.stop(sid)
        print(f"[ez] evicted owned twin for {sid[:8]} (terminal is the brain)", flush=True)
    if gc_ez.is_alive(name):
        return {"ok": True, "alive": True, "started": False}
    # `name` is the EZ handle; resume Claude by its real session id (== name for
    # unnamed/legacy sessions). Socket keeps the EZ handle either way.
    idx = _transcript_index()
    path = idx.get(sid)
    cwd = None
    if path is not None:
        _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    # Resume the CURRENT conversation — after /clear that's a rotated sid; resuming
    # the original would resurrect the conversation the user just cleared.
    resume = _effective_sid(path.parent.name, sid) if path is not None else sid
    gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", resume, *_name_flag(name),
                            "--permission-mode", "bypassPermissions"])
    return {"ok": True, "alive": gc_ez.is_alive(name), "started": True}


@app.post("/api/ez/{name}/refresh")
def ez_refresh(name: str):
    """Recreate the EZ terminal for this session WITHOUT losing the Claude
    conversation: kill the daemon holding the old PTY, then respawn it running
    `claude --resume <sid>` — the same transcript, back where we were, but on a
    fresh daemon (picks up engine fixes, clears a wedged/leaking PTY). The EZ name
    IS the Claude session id, so resume lands us in the identical session."""
    sid = sid_for_ez(name) or name
    # Evict any owned-stdin twin first (two resumes on one transcript diverge).
    if _sessions.is_owned_live(sid):
        _sessions.stop(sid)
    gc_ez.kill(name)
    # Wait for the socket to disappear so start() doesn't no-op on a stale one.
    for _ in range(40):
        if not gc_ez.is_alive(name):
            break
        time.sleep(0.05)
    idx = _transcript_index()
    path = idx.get(sid)
    cwd = None
    if path is not None:
        _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    # Resume the CURRENT conversation — after /clear that's a rotated sid; resuming
    # the original would undo the user's /clear by resurrecting the old one.
    resume = _effective_sid(path.parent.name, sid) if path is not None else sid
    gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", resume, *_name_flag(name),
                            "--permission-mode", "bypassPermissions"])
    return {"ok": True, "alive": gc_ez.is_alive(name)}


@app.get("/api/ez/list")
def ez_list():
    return {"sessions": gc_ez.list_sessions()}


@app.websocket("/ws/term/{name}")
async def ws_term(ws: WebSocket, name: str, cols: int = 80, rows: int = 40):
    """Bridge a browser xterm <-> the EZ PTY socket. Bytes both ways = the real
    terminal, live, with reliable keystroke delivery."""
    await ws.accept()
    print(f"[term] WS connect name={name!r} cols={cols} rows={rows}", flush=True)
    ezsock = gc_ez.connect_client(name, cols, rows)
    if ezsock is None:
        await ws.send_text("\r\n[no live terminal — /api/ez/{name}/ensure first]\r\n")
        await ws.close()
        return
    # Viewing the live terminal IS reviewing the session — clear its unread so the
    # repeat-alert stops. Sessions now open straight into the terminal view, which
    # never hits the chat GET that used to be the only thing clearing unread; that
    # gap made repeat-alerts (e.g. every 30s) buzz forever for an untouched session.
    try:
        clear_unread(sid_for_ez(name) or name, reason="ws-terminal-connect")
    except Exception:  # noqa: BLE001
        pass
    ezsock.setblocking(False)
    loop = asyncio.get_event_loop()

    # Fast open: on connect the daemon replays its ENTIRE ring buffer (up to 2MB). Having
    # xterm re-render all of it before the live prompt appears is the slow open on a heavy
    # session. Best practice (VS Code restores ~100 lines on reconnect, ttyd/tmux redraw the
    # current screen — nobody replays megabytes): forward only a RECENT TAIL of the opening
    # burst, then stream live. Full history stays in the session server-side; the live xterm
    # scrollback (5000 lines) refills as new output arrives.
    REPLAY_CAP = 256 * 1024                     # ~2000 lines — plenty of starting scrollback

    async def pump_out():
        try:
            # Phase 1 — drain the opening replay burst, keep only its tail. End on a quiet
            # gap (idle session) OR a 0.3s hard cap (a busy session never goes quiet).
            initial = bytearray()
            deadline = loop.time() + 0.3
            while loop.time() < deadline:
                try:
                    data = await asyncio.wait_for(loop.sock_recv(ezsock, 65536), timeout=0.1)
                except asyncio.TimeoutError:
                    break                       # burst drained
                if not data:
                    if initial:
                        await ws.send_bytes(bytes(initial[-REPLAY_CAP:]))
                    return
                initial += data
                if len(initial) > REPLAY_CAP * 4:
                    del initial[:-REPLAY_CAP]    # keep memory bounded during the drain
            if initial:
                await ws.send_bytes(bytes(initial[-REPLAY_CAP:]))
            # Phase 2 — live stream, forward every chunk immediately.
            while True:
                data = await loop.sock_recv(ezsock, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:  # noqa: BLE001
            pass

    out_task = asyncio.create_task(pump_out())
    # Engagement ack, THROTTLED and NON-BLOCKING. clear_unread reads+writes a JSON file
    # under a lock; calling it inline on the async loop for EVERY keystroke did dozens of
    # blocking disk reads/sec while typing, stalling the whole event loop (the app "locked
    # up" mid-session). Now: at most once per 2s per connection, and the file work runs in
    # a thread so it never blocks the loop. First keystroke still clears the alert promptly.
    last_ack = 0.0

    async def _ack_engagement():
        nonlocal last_ack
        now = time.monotonic()
        if now - last_ack < 2.0:
            return
        last_ack = now
        sid = sid_for_ez(name) or name
        await loop.run_in_executor(None, lambda: clear_unread(sid, "ws-terminal-input"))
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            t = msg.get("text")
            if b is not None:
                # ANY keystroke = Phil is engaging this session → acknowledge it (clears the
                # alert the moment he types in the terminal, not only on Enter).
                await _ack_engagement()
                if b'\r' in b or b'\n' in b:
                    mark_expecting(sid_for_ez(name) or name)
                await loop.sock_sendall(ezsock, b)
            elif t is not None:
                if t.startswith("{"):
                    try:
                        o = json.loads(t)
                        if o.get("t") == "resize":
                            # Ride the resize IN-BAND on this client's own socket so
                            # the daemon ties it to THIS client (tmux-style active
                            # sizing). ESC _ GCSZ;cols;rows ESC \ — stripped by the
                            # daemon, never reaches the PTY.
                            print(f"[term] resize name={name!r} -> cols={o.get('cols')} rows={o.get('rows')}", flush=True)
                            seq = f"\x1b_GCSZ;{int(o['cols'])};{int(o['rows'])}\x1b\\".encode()
                            await loop.sock_sendall(ezsock, seq)
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                await _ack_engagement()
                if "\r" in t or "\n" in t:
                    mark_expecting(sid_for_ez(name) or name)
                await loop.sock_sendall(ezsock, t.encode())
    except Exception:  # noqa: BLE001
        pass
    finally:
        out_task.cancel()
        try:
            ezsock.close()
        except OSError:
            pass


@app.get("/api/groundzero")
def groundzero():
    """Authoritative live truth: the real headless processes Ground Control owns,
    plus any live sessions running elsewhere (Claude desktop app / terminal)."""
    owned = _sessions.snapshot()
    owned_ids = set()
    for o in owned:
        title, proj = _session_display(o["sessionId"])
        o["title"] = title or o["sessionId"][:8]
        o["project"] = proj or ""
        owned_ids.add(o["sessionId"])
    external = []
    idx = _transcript_index()
    for sid, pid in live_sessions().items():
        if sid in owned_ids:
            continue
        path = idx.get(sid)
        try:
            mtime = path.stat().st_mtime if path else 0.0
        except OSError:
            mtime = 0.0
        title, proj = _session_display(sid)
        external.append({
            "sessionId": sid, "pid": pid,
            "title": title or sid[:8], "project": proj or "",
            "working": is_working(sid, True, mtime, False, path),
        })
    return {"now": time.time(), "server_pid": os.getpid(),
            "owned": owned, "external": external}


@app.post("/api/self-update")
def self_update():
    """Update an INSTALLED server (~/.ground-control) in place from the public repo,
    then exit so launchd restarts it on the new code. The Mac app's update banner
    calls this before swapping the app — one click updates everything. Phil's dev
    server (repo checkout) declines; it's updated by editing the source."""
    install_dir = Path(__file__).resolve().parent
    if install_dir != (Path.home() / ".ground-control"):
        return {"ok": False, "reason": "dev_server"}
    import io
    import tarfile
    import tempfile
    import urllib.request as _ur
    try:
        url = "https://github.com/PhilipBuonforte/ground-control-server/archive/refs/heads/main.tar.gz"
        data = _ur.urlopen(url, timeout=30).read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tmp = Path(tempfile.mkdtemp(prefix="gc-selfupdate-"))
            tar.extractall(tmp)   # noqa: S202 — our own repo tarball
            src = next(tmp.glob("ground-control-server-*"))
            for f in ["server.py", "gc_ez.py", "gc_ez_engine.py", "gc_sessions.py",
                      "run_server.sh", "requirements.txt"]:
                if (src / f).exists():
                    shutil.copy(src / f, install_dir / f)
            for d in ["static", "ezterminfo"]:
                if (src / d).exists():
                    shutil.rmtree(install_dir / d, ignore_errors=True)
                    shutil.copytree(src / d, install_dir / d)
            hook = src / "hooks" / "pocket-claude-notify.py"
            if hook.exists():
                (Path.home() / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
                shutil.copy(hook, Path.home() / ".claude" / "hooks" / "pocket-claude-notify.py")
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    # Exit AFTER the response flushes; launchd brings us back on the new code.
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"ok": True, "restarting": True}


# --- Adopt existing sessions into the app ------------------------------------
# Sessions born OUTSIDE Ground Control (a plain `claude` in Terminal.app) can be
# brought in: dormant ones are marked owned + get a sidebar record, and wake as
# their own EZ terminal on first open. Ones still RUNNING elsewhere are refused —
# attaching would `--resume` a fork of a live conversation (never fork; invariant).


@app.get("/api/adoptable")
def adoptable(limit: int = 30):
    """Recent sessions on this Mac that the app doesn't own yet."""
    owned = _load_owned()
    ext_running = set(live_sessions()) - set(_sessions.owned_live_ids())
    out = []
    for sid, path in _transcript_index().items():
        if sid in owned:
            continue
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if time.time() - m > 30 * 86400:
            continue
        # /clear-rotation children of sessions we already own are not new sessions.
        if _stable_sid(sid, str(path)) in owned:
            continue
        title, preview, cwd = session_meta(path)
        label = (title or preview or "").strip().replace("\n", " ")[:60] or sid[:8]
        out.append({"id": sid, "dir": path.parent.name, "title": label,
                    "cwd": cwd or "", "mtime": m,
                    "running_elsewhere": sid in ext_running})
    out.sort(key=lambda x: -x["mtime"])
    return {"sessions": out[:limit]}


class AdoptIntoAppBody(BaseModel):
    id: str


@app.post("/api/adopt-into-app")
def adopt_into_app(body: AdoptIntoAppBody):
    """Bring a dormant outside session into the app: owned + sidebar record.
    It wakes as its own EZ terminal the first time it's opened (terminal-wake)."""
    sid = body.id
    ext_running = set(live_sessions()) - set(_sessions.owned_live_ids())
    if sid in ext_running:
        return JSONResponse(
            {"ok": False, "reason": "running_elsewhere",
             "note": "This session is open in another terminal. Quit it there first "
                     "(type 'exit' or press Ctrl+D), then adopt it."},
            status_code=409)
    path = _transcript_index().get(sid)
    if path is None:
        return JSONResponse({"ok": False, "reason": "not_found"}, status_code=404)
    title, preview, cwd = session_meta(path)
    name = ((title or preview or "Adopted session").strip().replace("\n", " "))[:60]
    # Reuse an existing record (un-archive it) rather than forging a duplicate —
    # two records for one sid makes archive/rename appear to "not work" (one gets
    # marked, the sidebar reads the other).
    f, d = _find_record_file(sid)
    if f:
        d["isArchived"] = False
        _set_archived(d.get("cliSessionId") or "", False)
        json.dump(d, open(f, "w"))
    else:
        _forge_desktop_record(sid, cwd or str(Path.home()), name)
    _add_owned(sid)
    mark_expecting(sid)   # the user explicitly pulled it in → alerts allowed
    return {"ok": True, "title": name}


@app.post("/api/session/{session_id}/adopt")
def adopt_session(session_id: str):
    """Take any session into HEADLESS ownership so the Live view + sync work on it.
    Already-owned → no-op. Idle-elsewhere (terminal/desktop) → release + adopt.
    Actively working elsewhere → refuse (don't corrupt a live turn)."""
    if _sessions.is_owned_live(session_id):
        s = _sessions.get(session_id)
        return {"ok": True, "headless": True, "via": "already-owned",
                "pid": s.proc.pid if s and s.proc else None}
    idx = _transcript_index()
    path = idx.get(session_id)
    cwd = None
    if path is not None:
        _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    external = set(live_sessions()) - set(_sessions.owned_live_ids())
    if session_id in external:
        try:
            mtime = path.stat().st_mtime if path else 0.0
        except OSError:
            mtime = 0.0
        if is_working(session_id, True, mtime, False, path):
            return JSONResponse(
                {"ok": False, "reason": "busy_elsewhere",
                 "note": "This session is actively running in a terminal/desktop right now. "
                         "Try again when it's idle so nothing is interrupted."},
                status_code=409)
        if not release_session(session_id):
            return JSONResponse(
                {"ok": False, "reason": "release_failed",
                 "note": "Couldn't release the other process."}, status_code=409)
    s = _sessions.adopt(session_id, cwd)
    return {"ok": True, "headless": True, "via": "adopted",
            "pid": s.proc.pid if s and s.proc else None}


@app.get("/api/live/{session_id}")
def live_feed(session_id: str, since: int = 0):
    """Real-time activity of a headless session — the exact text/tool-calls it's
    producing, for the app's Live view. Poll with the last `seq` you've seen."""
    s = _sessions.get(session_id)
    if not s or not s.is_live():
        # Always include pid+uptime (even null/0) so the app's decoder succeeds
        # and shows the clean "not headless" empty state instead of spinning.
        return {"live": False, "busy": False, "seq": 0, "pid": None,
                "uptime": 0, "events": []}
    evs = [e for e in list(s.activity) if e["seq"] > since]
    return {
        "live": True, "busy": bool(s.busy), "seq": s.activity_seq,
        "pid": s.proc.pid if s.proc else None,
        "uptime": int(time.time() - s.started_at) if s.started_at else 0,
        "events": evs,
    }


DESKTOP_DIR = Path.home() / "Library" / "Application Support" / "Claude"
DESKTOP_CONFIG = DESKTOP_DIR / "claude_desktop_config.json"


def _ensure_desktop_scaffold():
    """Ground Control reuses the Claude DESKTOP app's session-record format as its
    session registry (records are forged by _forge_desktop_record). A Mac with only
    Claude CODE — i.e. every fresh install by a new user — has none of those files.
    Bootstrap an empty scaffold so every reader works identically whether or not
    the desktop app is installed. No-op when it already exists."""
    try:
        (DESKTOP_DIR / "claude-code-sessions").mkdir(parents=True, exist_ok=True)
        if not DESKTOP_CONFIG.exists():
            json.dump({"preferences": {"epitaxyPrefs": {"dframe-local-slice": {}}}},
                      open(DESKTOP_CONFIG, "w"), indent=2)
    except OSError:
        pass


_ensure_desktop_scaffold()


def _desktop_config() -> dict:
    """The desktop config, never raising — a stranger's Mac may lack the file
    entirely (bootstrapped above), and a half-written file must not 500 the
    session list."""
    try:
        return json.load(open(DESKTOP_CONFIG))
    except (OSError, json.JSONDecodeError):
        return {}


def _transcript_index():
    """sessionId -> transcript path, across all project dirs."""
    idx = {}
    for f in PROJECTS_DIR.glob("*/*.jsonl"):
        if not f.name.startswith("agent-"):
            idx[f.stem] = f
    return idx


def _desktop_records():
    """localId ('code:local_x') -> desktop session record."""
    recs = {}
    for f in DESKTOP_DIR.glob("claude-code-sessions/*/*/local_*.json"):
        try:
            d = json.load(open(f))
            recs["code:" + d["sessionId"]] = d
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return recs


@app.get("/api/sessions")
def list_sessions():
    live = live_sessions()
    idx = _transcript_index()
    recs = _desktop_records()

    cfg = _desktop_config()
    sl = ((cfg.get("preferences") or {}).get("epitaxyPrefs") or {}).get("dframe-local-slice") or {}
    assign = sl.get("customGroupAssignments", {})
    group_order = sl.get("customGroupOrder", {})

    names_path = Path(__file__).parent / "cg_names.json"
    cg_names = {"order": [], "names": {}}
    if names_path.exists():
        try:
            cg_names = json.load(open(names_path))
        except json.JSONDecodeError:
            pass
    ezmap = _load_ez_names()
    owned = _load_owned()
    try:
        ez_live = set(gc_ez.list_sessions())
    except Exception:
        ez_live = set()

    gc_archived = _archived_set()

    def build_session(local_id):
        r = recs.get(local_id)
        if not r:
            return None
        sid = r.get("cliSessionId")
        if sid not in owned:
            return None  # app only shows sessions born in it, not the desktop-app world
        if sid in gc_archived:
            return None  # GC-archived is FINAL until explicitly unarchived (adopt)
        # NOTE: we deliberately IGNORE the desktop app's `isArchived` flag — it
        # auto-archives fresh 0-turn sessions (a session created with no first
        # message) and resurrects others, so it can't be trusted. GC's own
        # archived.json above is the only archive truth.
        is_live = (sid in live) or (ezmap.get(sid) in ez_live)
        path = idx.get(sid)
        if path is not None:
            try:
                _, preview, _ = session_meta(path)
                mtime = path.stat().st_mtime
            except OSError:
                return None
            dir_name = path.parent.name
        elif is_live:
            # Live session with NO transcript yet — freshly created with no first
            # message, so Claude hasn't written a .jsonl. Show it anyway (empty), with
            # the project-dir slug derived from its cwd so send/resume still target it.
            preview = ""
            mtime = (r.get("lastActivityAt") or int(time.time() * 1000)) / 1000.0
            cwd_slug = r.get("cwd") or ""
            dir_name = re.sub(r'[^A-Za-z0-9]', '-', cwd_slug) if cwd_slug else sid
        else:
            return None
        with _jobs_lock:
            job = _jobs.get(sid, {})
        return {
            "id": sid,
            "ezName": ezmap.get(sid, sid),
            "resumeName": sid,
            "dir": dir_name,
            "title": (r.get("title") or "Untitled")[:80],
            "preview": (preview or "")[:120],
            "project": Path(r.get("cwd") or "").name,
            "cwd": r.get("cwd") or "",
            "mtime": mtime,
            "live": sid in live,
            # The authoritative instantaneous fact: is this session emitting output right now
            # (the SAME computation /api/work uses, keyed by the SAME EZ handle, so surfaces
            # can't disagree). The DECAY that smooths gaps + survives dropped polls lives on
            # the CLIENT, timed by the client's OWN clock — never a cross-machine timestamp
            # comparison (that clock-skew mistake made the spinner vanish on the phone).
            "busy": working_by_ez(ezmap.get(sid, sid)),
            "unread": sid in unreads,
        }

    unreads = _load_unreads()
    groups = []
    used = set()
    for cg, members in group_order.items():
        sessions = []
        for m in members:
            used.add(m)
            s = build_session(m)
            if s:
                sessions.append(s)
        groups.append(
            {"name": cg_names["names"].get(cg, "New Group"), "sessions": sessions}
        )
    # any assigned-but-not-ordered members
    for m, cg in assign.items():
        if m not in used:
            used.add(m)
            s = build_session(m)
            if s:
                name = cg_names["names"].get(cg, "New Group")
                g = next((g for g in groups if g["name"] == name), None)
                if g is None:
                    g = {"name": name, "sessions": []}
                    groups.append(g)
                g["sessions"].append(s)

    # ungrouped = desktop sessions with no group assignment
    ungrouped = []
    for local_id in recs:
        if local_id not in used:
            s = build_session(local_id)
            if s:
                ungrouped.append(s)
    ungrouped.sort(key=lambda s: -s["mtime"])

    order = cg_names.get("order", [])
    groups.sort(key=lambda g: order.index(g["name"]) if g["name"] in order else 99)
    if ungrouped:
        groups.append({"name": "Ungrouped", "sessions": ungrouped})
    # Empty groups STAY VISIBLE (Phil: "a group should not disappear just because it
    # has no sessions in it") — they're still drop targets and organizational anchors.
    # Only the synthetic Ungrouped bucket hides when empty (guarded above).
    # Keep the icon badge honest: prune any unread whose session isn't shown here.
    reconcile_unreads({s["id"] for g in groups for s in g["sessions"]})
    return {"groups": groups}


def _pending_question(path):
    """Pull the pending AskUserQuestion (question text + options) out of the
    transcript tail so chat can render the actual prompt with exact labels.

    Reality check (measured on FA: Marketing): the CLI may not flush the tool_use
    line to disk until the question is ANSWERED — so while the menu is up, the
    transcript often doesn't contain it yet and this returns None; get_session then
    reads the live terminal (gc_ez.terminal_question). This path still matters: it
    wins whenever the line IS present (exact labels + descriptions + multiSelect).

    Hardening (all bit us or nearly did):
    - Read only the tail BYTES. This ran path.read_text() on a 66MB transcript every
      1-second poll — ~130MB of string churn per poll for 40 lines of interest.
    - errors="replace": the file is being APPENDED mid-read; a truncated multibyte
      char raised UnicodeDecodeError, which except OSError never caught → a 500.
    - NEVER return an ANSWERED question. The gate ("terminal is blocked on a prompt")
      is live but this tail is history — an old answered AskUserQuestion sitting here
      while a DIFFERENT prompt is up would show stale labels whose tapped digit
      answers the wrong menu. If the tail contains a tool_result for this tool_use
      id, it's history — skip it."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512_000))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()[1:] if raw else []   # first line is likely partial — drop it
    answered = set()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if '"tool_result"' in line or '"AskUserQuestion"' in line:
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    answered.add(c.get("tool_use_id"))
                elif c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                    if c.get("id") in answered:
                        return None       # newest question is already answered → nothing pending
                    qs = (c.get("input") or {}).get("questions") or []
                    if not qs:
                        continue
                    q = qs[0]
                    return {
                        "header": q.get("header", ""),
                        "question": q.get("question", ""),
                        "multiSelect": bool(q.get("multiSelect")),
                        "options": [{"label": o.get("label", ""), "description": o.get("description", "")}
                                    for o in (q.get("options") or [])],
                    }
    return None


@app.get("/api/session/{project_dir}/{session_id}")
def get_session(project_dir: str, session_id: str, limit: int = 80):
    # Follow /clear rotations: read the session's CURRENT conversation transcript,
    # not the pre-clear one (the stable GC identity stays `session_id`).
    eff = _effective_sid(project_dir, session_id)
    path = PROJECTS_DIR / project_dir / f"{eff}.jsonl"
    if not path.exists():
        path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    # NOTE: do NOT clear_unread here — the app POLLS this endpoint every ~1s while a
    # session is on screen, which passively marked it "read" and killed repeat alerts
    # even when Phil wasn't looking. "Read" = an ACTIVE action: opening the terminal
    # (WS connect → clear_unread), replying (mark_expecting → clear_unread), or tapping
    # the alert (navigates → opens → clear). A screen merely showing it is not enough.
    turns = parse_turns(path)
    with _jobs_lock:
        job = _jobs.get(session_id, {})
    mtime = path.stat().st_mtime
    live = session_id in live_sessions()
    # Actively-viewed session → read the terminal live (snapshot) so busy tracks
    # the terminal instantly, e.g. clears the moment STOP quiets it.
    # NEVER trust the legacy `_jobs` status string (job_running=False) — a stale
    # "running" that never cleared is exactly the phantom spinner. terminal_snapshot=True
    # reads the live terminal for the viewed session so busy tracks it instantly.
    busy = is_working(session_id, live, mtime, False, path, terminal_snapshot=True)
    ez = ez_name_for(session_id)
    # work.label = Claude's own status line, read from the terminal ("Brewed · 1
    # shell still running", "Julienning…") — the single source of truth. Clean timer
    # + tokens still come from the transcript (the terminal's own timer is unreliable).
    work = None
    if busy:
        work = _work_progress(path) or {"seconds": 0, "tokens": 0}
        if gc_ez.is_alive(ez):
            _, label = gc_ez.work_status(ez)
            if label:
                work["label"] = label
    # "waiting" = the terminal is BLOCKED on an interactive prompt (question /
    # permission / trust). Only possible when NOT working, so skip the read if busy.
    waiting = None
    waiting_question = None
    if not busy and gc_ez.is_alive(ez):
        waiting = gc_ez.waiting_for_input(ez)
        if waiting:
            # Prefer the STRUCTURED tool call (exact labels + descriptions). If it isn't an
            # AskUserQuestion — a permission prompt, a trust prompt, one of Claude's own
            # numbered menus — read the numbered options straight off the terminal screen so
            # the answer is STILL tappable in the app. Falling back to a dead-end "Answer in
            # Terminal" button is the failure mode Phil hates: the point of the app is that
            # you are never punted into the terminal to get unstuck.
            # The hook cache is ONLY valid for an actual question prompt — a
            # lingering cache entry must never label a permission/trust prompt
            # with a previous question's options (typed digit would answer the
            # wrong thing).
            hook_q = _hook_question(session_id, eff) if waiting == "question" else None
            waiting_question = (hook_q
                                or _pending_question(path)
                                or gc_ez.terminal_question(ez))
    # The chat used to hard-truncate to the last 80 turns, so a long session showed a
    # fraction of itself while the terminal kept the whole scrollback — "the chat and
    # the terminal don't match" (FA Markting 2: 380 turns, 300 of them invisible).
    # Now the window is client-driven: it polls with the default and asks for more when
    # you scroll back, so the live poll stays small but no history is unreachable.
    # `total` tells the app whether older turns exist.
    window = max(20, min(int(limit or 80), 2000))
    return {
        "turns": turns[-window:],
        "total": len(turns),
        "live": live,
        "busy": busy,
        "waiting": waiting,
        "waitingQuestion": waiting_question,
        "work": work,
        "job": {k: job.get(k) for k in ("status", "error")},
        "mtime": mtime,
    }


class TypeBody(BaseModel):
    text: str = ""   # raw text typed into the terminal (NO trailing enter)


@app.post("/api/session/{project_dir}/{session_id}/type")
def type_into_terminal(project_dir: str, session_id: str, body: TypeBody):
    """Type raw text into the session's terminal WITHOUT pressing enter — used to
    drop an uploaded file's path into the input so Claude can read it (the user then
    adds context and hits send). Terminal stays the brain."""
    ez = ez_name_for(session_id)
    if not gc_ez.is_alive(ez):
        return JSONResponse({"ok": False, "error": "no live terminal"}, status_code=400)
    gc_ez.send_input(ez, body.text)
    return {"ok": True}


class AnswerBody(BaseModel):
    index: int = 0   # which option (0-based, in the order chat shows them)


# EXACT pending-question cache, fed by the PreToolUse hook. The transcript
# doesn't flush AskUserQuestion until it's ANSWERED, so chat used to fall back
# to screen-scraping the TUI — which the new two-column question UI turns into
# garbage (truncated labels, box-border pipes, "N lines hidden"). The hook
# fires BEFORE the tool runs with the full question JSON: perfect fidelity.
_question_cache = {}   # stable sid -> {"ts": epoch, "q": {...}}
_QUESTION_TTL = 3600.0


def _hook_question(*sids):
    now = time.time()
    for sid in sids:
        e = _question_cache.get(sid)
        if e and now - e["ts"] < _QUESTION_TTL:
            return e["q"]
    return None


class QuestionEvent(BaseModel):
    session_id: str
    raw_session_id: Optional[str] = None
    tool_input: dict = {}


@app.post("/api/question-event")
def question_event(body: QuestionEvent):
    qs = (body.tool_input or {}).get("questions") or []
    if not qs:
        return {"ok": False}
    q = qs[0]
    sid = _stable_sid(body.session_id)
    _question_cache[sid] = {"ts": time.time(), "q": {
        "header": q.get("header", ""),
        "question": q.get("question", ""),
        "multiSelect": bool(q.get("multiSelect")),
        "options": [{"label": o.get("label", ""), "description": o.get("description", "")}
                    for o in (q.get("options") or [])],
    }}
    return {"ok": True}


@app.post("/api/session/{project_dir}/{session_id}/answer")
def answer_question(project_dir: str, session_id: str, body: AnswerBody):
    """Answer a pending AskUserQuestion FROM CHAT (single-select). The prompt opens
    with the first option highlighted, so navigate down `index` times and Enter —
    exactly the keystrokes Phil would type in the terminal. Terminal stays the brain."""
    ez = ez_name_for(session_id)
    if not gc_ez.is_alive(ez):
        return JSONResponse({"ok": False, "error": "no live terminal"}, status_code=400)
    # ANY blocked prompt is answerable by number (question / permission / trust / menu) —
    # not just AskUserQuestion. Gating on == "question" was what forced everything else into
    # the dead-end "Answer in Terminal" path.
    if not gc_ez.waiting_for_input(ez):
        return JSONResponse({"ok": False, "error": "not waiting on a prompt"}, status_code=409)
    # Claude Code selects accept the option NUMBER directly (verified: sending "1"
    # picks + confirms the first option; arrow-key injection did NOT navigate). The
    # options are numbered 1..N in the same order chat shows them, so 0-based index
    # → the 1-based digit. (Works for the ≤9 options AskUserQuestion ever shows.)
    gc_ez.send_input(ez, str(body.index + 1))
    mark_expecting(session_id)
    return {"ok": True, "index": body.index}


UPLOADS_DIR = Path(__file__).parent / "uploads"


@app.post("/api/session/{project_dir}/{session_id}/upload")
async def upload_file(project_dir: str, session_id: str, request: Request, name: str = "file"):
    safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:80] or "file"
    dest_dir = UPLOADS_DIR / session_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    import uuid as _uuid

    dest = dest_dir / f"{int(time.time())}_{_uuid.uuid4().hex[:6]}_{safe}"
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    dest.write_bytes(body)
    return {"path": str(dest)}


class SendBody(BaseModel):
    text: str = ""
    attachments: list[str] = []


_queues = {}          # session_id -> [text, ...] pending messages
_queue_workers = set()  # session_ids with an active worker thread
_queue_lock = threading.Lock()


def _build_text(raw: str, attachments):
    text = (raw or "").strip()
    valid = [p for p in attachments if Path(p).resolve().is_relative_to(UPLOADS_DIR.resolve()) and Path(p).exists()]
    if valid:
        listing = "\n".join(f"- {p}" for p in valid)
        text += f"\n\n[The user attached {len(valid)} file(s) from their phone — Read them:]\n{listing}"
    return text.strip()


def _queue_worker(session_id: str, cwd: str):
    """Process a session's queued messages one at a time, in order."""
    while True:
        with _queue_lock:
            q = _queues.get(session_id) or []
            if not q:
                _queue_workers.discard(session_id)
                return
            text = q.pop(0)
        with _jobs_lock:
            _jobs[session_id] = {"status": "running", "started": time.time()}
        if not release_session(session_id):
            with _jobs_lock:
                _jobs[session_id] = {"status": "error", "error": "could not release the session"}
            with _queue_lock:
                _queue_workers.discard(session_id)
            return
        _run_injection(session_id, cwd, text, True)  # blocking


CHANNELS_DIR = Path.home() / ".ground-control" / "channels"


def _live_channel_port(session_id: str):
    """If this session has a live Ground Control channel (a `claude` started with
    the channel flag), return its local port. Delivering through the channel drops
    the message into the RUNNING session with no kill/resume. Returns None if there
    is no channel or its process is dead (→ caller falls back to kill-then-resume)."""
    f = CHANNELS_DIR / f"{session_id}.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        return None
    pid = d.get("pid")
    if pid:
        try:
            os.kill(pid, 0)  # process alive?
        except OSError:
            try:
                f.unlink()  # stale registration → clean up
            except OSError:
                pass
            return None
    return d.get("port")


# Focus the target session in the Claude desktop sidebar by its title, then paste
# the message and press Enter. Returns "sent" only if the sidebar row was found and
# clicked (so we never type into the WRONG session); "notfound" otherwise.
_DESKTOP_TYPE_SCRIPT = '''
on run argv
    set targetTitle to item 1 of argv
    set msg to item 2 of argv
    tell application "Claude" to activate
    delay 0.5
    tell application "System Events" to tell process "Claude"
        set rowPos to missing value
        set inputPos to missing value
        -- one scan: locate the sidebar row AND the input box
        try
            repeat with e in (entire contents of front window)
                try
                    set r to role of e
                    if r is "AXStaticText" and rowPos is missing value and ((value of e) as string) is targetTitle then
                        set {px, py} to position of e
                        set {sw, sh} to size of e
                        set rowPos to {px + (sw div 2), py + (sh div 2)}
                    else if r is "AXTextArea" and inputPos is missing value then
                        set {qx, qy} to position of e
                        set {qw, qh} to size of e
                        set inputPos to {qx + (qw div 2), qy + (qh div 2)}
                    end if
                end try
            end repeat
        end try
        if rowPos is missing value then return "notfound"
        click at rowPos          -- switch to the target session
        delay 0.7                -- let the view swap in
        set the clipboard to msg
        if inputPos is not missing value then click at inputPos  -- focus the input
        delay 0.2
        keystroke "v" using command down
        delay 0.35
        key code 36              -- Enter = send
    end tell
    return "sent"
end run
'''


def _desktop_app_running() -> bool:
    try:
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "Claude"'],
            text=True, timeout=5).strip()
        return out == "true"
    except Exception:
        return False


def _session_title(session_id: str) -> str:
    _, d = _find_record_file(session_id)
    return (d or {}).get("title") or ""


def _type_into_desktop(text: str, title: str) -> bool:
    """Focus the target session by title in the Claude desktop sidebar, then paste +
    send. The app does the turn itself → shows natively, no kill/resume. Returns
    False if the session's sidebar row wasn't found (caller falls back)."""
    if not title:
        return False
    try:
        out = subprocess.run(["osascript", "-e", _DESKTOP_TYPE_SCRIPT, title, text],
                             capture_output=True, text=True, timeout=25)
        return out.stdout.strip() == "sent"
    except Exception:
        return False


def _wake_ez_and_send(session_id: str, text: str):
    """Wake a DORMANT session as its OWN EZ terminal — never a headless
    `--resume` twin. Same session id, same transcript, exactly one live process.

    The EZ wrapper is disposable plumbing; the Claude session is the identity we
    preserve. If the user wanted a NEW Claude they'd hit New Session — so waking a
    dormant row must land them back in the SAME conversation, as its single live
    terminal (the shareable one-stream form it was born in), not a divergent copy.

    Runs in a background thread: a resumed Claude takes a few seconds to boot its
    TUI before it will accept typed input."""
    name = ez_name_for(session_id)
    # Never two heads on one transcript: evict any owned-stdin twin first.
    if _sessions.is_owned_live(session_id):
        _sessions.stop(session_id)
    if not gc_ez.is_alive(name):
        path = _transcript_index().get(session_id)
        cwd = None
        if path is not None:
            _, _, cwd = session_meta(path)
        if not cwd or not os.path.isdir(cwd):
            cwd = str(Path.home())
        # Resume the CURRENT conversation (follows /clear rotations).
        resume = _effective_sid(path.parent.name, session_id) if path is not None else session_id
        gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", resume, *_name_flag(name),
                                "--permission-mode", "bypassPermissions"])

    def _norm(b: bytes) -> str:
        # A TUI positions text with cursor moves; strip ANSI + whitespace to match.
        s = b.decode("utf-8", "replace")
        s = _re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
        s = _re.sub(r"\x1b\][^\x07]*(\x07|\x1b\\)", "", s)
        return _re.sub(r"\s+", "", s).lower()

    # Wait for the resumed input box to render before typing (else the keystrokes
    # land mid-boot and get eaten). Resume skips the trust gate (folder already
    # trusted), so we only watch for the ready markers.
    for _ in range(60):  # ~30s ceiling
        time.sleep(0.5)
        if not gc_ez.is_alive(name):
            continue
        snap = _norm(gc_ez.snapshot(name, 100, 40))
        if "bypasspermissions" in snap or "?forshortcuts" in snap:
            break
    time.sleep(0.4)
    if not gc_ez.is_alive(name):
        return
    # Type the text, THEN Enter as a separate keystroke (a single fast write is
    # treated as a paste, so the CR lands as a literal newline, not a submit).
    if gc_ez.send_input(name, text):
        time.sleep(0.12)
        gc_ez.send_input(name, "\r")


class ModelBody(BaseModel):
    model: str   # alias ("opus"|"fable"|"sonnet"|"haiku") or a full id


# sid → (label, set_at). What the TERMINAL said the model is, after an app-initiated
# switch. The transcript can't answer this until the next assistant message exists, so
# this bridges the gap; the transcript wins again as soon as it has something newer.
_MODEL_OVERRIDE: dict = {}
_model_override_lock = threading.Lock()


# Aliases the Claude CLI accepts for `/model` (see `claude --help`: "Provide an alias
# for the latest model (e.g. 'fable', 'opus', or 'sonnet') or a model's full name").
MODEL_ALIASES = ["opus", "fable", "sonnet", "haiku"]


@app.post("/api/session/{project_dir}/{session_id}/model")
def set_model(project_dir: str, session_id: str, body: ModelBody):
    """Switch a session's model by typing `/model <alias>` into ITS OWN terminal.

    Terminal is the brain: the model lives in that Claude process, so the only honest
    way to change it is the same slash command a human would type. Deliberately NOT
    routed through /send — this must not mark the session as expecting a reply (that
    would arm alerts for a settings tweak).
    """
    alias = (body.model or "").strip().lower()
    if not alias or (alias not in MODEL_ALIASES and not alias.startswith("claude-")):
        return JSONResponse({"error": f"unknown model '{body.model}'"}, status_code=400)
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    ez = ez_name_for(session_id)
    if not gc_ez.is_alive(ez):
        return JSONResponse(
            {"error": "no live terminal for this session — open it once, then switch"},
            status_code=409)
    # Same two-step as /send: text first, CR as its own keystroke a beat later, or
    # Claude's Ink TUI treats it as a paste and the newline never submits.
    ok = gc_ez.send_input(ez, f"/model {alias}")
    if ok:
        time.sleep(0.12)
        ok = gc_ez.send_input(ez, "\r")
    if not ok:
        return JSONResponse({"ok": False, "note": "terminal busy — try again"}, status_code=409)
    # `/model` does NOT switch silently when the conversation is cached — Claude asks
    # "Switch model? 1. Yes … 2. No" and WAITS. Left unanswered the session sits blocked
    # and the model never changes (looks like the button did nothing). Auto-answer "1" so
    # one tap in the app is genuinely one action. Poll briefly: the prompt takes a moment
    # to render, and it doesn't appear at all when the model is already active.
    confirmed = False
    for _ in range(14):                      # ~2.8s
        time.sleep(0.2)
        lines = gc_ez._render_lines(ez) or []      # pyte-rendered visible grid
        flat = "".join("".join(lines).split()).lower()
        if "switchmodel?" in flat or "yes,switchto" in flat:
            gc_ez.send_input(ez, "1")
            time.sleep(0.12)
            gc_ez.send_input(ez, "\r")
            confirmed = True
            break
    # Read the model back OFF THE TERMINAL ("⎿ Set model to Opus 4.8 …") and remember it.
    # Without this the app kept showing the OLD model after a successful switch: the
    # label is derived from the last assistant message in the transcript, and no new
    # message exists yet — so the switch looked like it did nothing (Phil's exact report).
    # Take the LAST match and require it to name the model we actually asked for.
    # Older "Set model to …" lines from previous switches are still on screen, and
    # matching one of those reports the WRONG model — worse than reporting none.
    label = ""
    for _ in range(12):                      # ~2.4s for the confirmation line to render
        time.sleep(0.2)
        lines = gc_ez._render_lines(ez) or []
        flat = "".join("".join(lines).split())
        hits = _re.findall(r"[Ss]etmodelto([A-Za-z]+[\d.]*)", flat)
        if hits and hits[-1].lower().startswith(alias):
            raw = hits[-1]
            mm = _re.match(r"([A-Za-z]+)([\d.]*)", raw)
            label = f"{mm.group(1)} {mm.group(2)}".strip() if mm else raw
            break
    if label:
        with _model_override_lock:
            _MODEL_OVERRIDE[session_id] = (label, time.time())
    return {"ok": True, "model": alias, "confirmed": confirmed, "label": label}


@app.get("/api/fs")
def fs_list(path: str = ""):
    """Browse the host Mac's files so the phone can attach one directly — the
    file already lives on the server Mac, so attaching is just its path (no
    upload). Hidden files skipped; capped listing; home when no path given."""
    base = Path(path).expanduser() if path.strip() else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            return JSONResponse({"error": "not a directory"}, status_code=404)
        entries = []
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append({"name": p.name, "path": str(p),
                            "dir": p.is_dir(), "size": int(st.st_size),
                            "mtime": st.st_mtime})
            if len(entries) >= 400:
                break
        return {"path": str(base),
                "parent": None if base == base.parent else str(base.parent),
                "entries": entries}
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/fs/search")
def fs_search(q: str, root: str = ""):
    """Recursive filename search under `root` (home by default) for the phone's
    Mac-file browser. Bounded hard: skips hidden/heavy dirs, depth ≤ 6, ≤ 200
    results, ≤ 3s — a search can never wedge the server."""
    base = Path(root).expanduser() if root.strip() else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            return JSONResponse({"error": "not a directory"}, status_code=404)
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ql = q.lower().strip()
    if not ql:
        return {"path": str(base), "parent": None, "entries": []}
    skip = {"node_modules", "Library", ".git", "venv", ".venv", "__pycache__",
            "DerivedData", "Pods", "Movies", "Music"}
    out, t0 = [], time.time()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip]
        try:
            if len(Path(dirpath).relative_to(base).parts) >= 6:
                dirnames[:] = []
        except ValueError:
            pass
        for name in dirnames + filenames:
            if name.startswith(".") or ql not in name.lower():
                continue
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": name, "path": str(p), "dir": p.is_dir(),
                        "size": int(st.st_size), "mtime": st.st_mtime})
            if len(out) >= 200:
                break
        if len(out) >= 200 or time.time() - t0 > 3.0:
            break
    return {"path": str(base), "parent": None, "entries": out}


_SENT_JOURNAL = Path.home() / ".ground-control" / "sent-messages.jsonl"


def _journal_send(sid: str, text: str):
    """Every outbound message is journaled BEFORE any routing. A TUI accident
    (menu swallowing keystrokes, a crashed terminal) can eat the delivery, but
    never the words — Hank lost a hand-written answer this way once."""
    try:
        _SENT_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with open(_SENT_JOURNAL, "a") as f:
            f.write(json.dumps({"ts": time.time(), "sid": sid, "text": text}) + "\n")
    except Exception:
        pass


@app.post("/api/session/{project_dir}/{session_id}/send")
def send_message(project_dir: str, session_id: str, body: SendBody):
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    text = _build_text(body.text, body.attachments)
    _journal_send(session_id, text)
    mark_expecting(session_id)   # Phil sent → allow this session to alert him about the result
    # TERMINAL IS THE BRAIN: if this session is running as a live EZ terminal, that
    # PTY is the one true process — type into it. NEVER fall through to the owned/
    # takeover path below (which would kill the terminal to spawn a headless twin).
    ez = ez_name_for(session_id)
    if gc_ez.is_alive(ez):
        # Terminal is the brain — evict any owned-stdin twin resuming this same
        # session so the two don't diverge on one transcript (chat/terminal sync).
        if _sessions.is_owned_live(session_id):
            _sessions.stop(session_id)
        # INTERACTIVE MENU GUARD: if a question/permission dialog is on screen,
        # typed text is eaten as menu navigation and the trailing Enter just
        # accepts the highlighted option — Hank lost a multi-sentence custom
        # answer exactly this way. Esc dismisses the dialog first (Claude sees
        # "declined"), THEN the text lands in the real composer as his answer.
        try:
            if gc_ez.waiting_for_input(ez):
                gc_ez.send_input(ez, "\x1b")
                for _ in range(10):
                    time.sleep(0.25)
                    if not gc_ez.waiting_for_input(ez):
                        break
        except Exception:
            pass
        # Type the text, THEN send Enter as a separate keystroke after a short
        # beat. Claude's Ink TUI treats a single fast write (text + "\r") as a
        # paste, so the trailing CR lands as a literal newline in the input box
        # instead of submitting — the message just sits there until you press
        # Enter yourself. Splitting it = the CR registers as a real submit.
        ok = gc_ez.send_input(ez, text)
        if ok:
            time.sleep(0.12)
            ok = gc_ez.send_input(ez, "\r")
        return {"ok": ok, "via": "terminal"} if ok else JSONResponse(
            {"ok": False, "via": "terminal", "note": "terminal busy — try again"},
            status_code=409)
    # PRIMARY: Ground Control owns this session's process → write to stdin. 100%
    # reliable, no channel/AX/kill-resume, live streaming for free. If we don't
    # own it yet, adopt it (spawn `--resume`) UNLESS another process already owns
    # it live (e.g. the Claude desktop app) — only then fall back to legacy paths.
    if _sessions.is_owned_live(session_id):
        try:
            _sessions.send(session_id, cwd, text)
            return {"ok": True, "via": "owned-stdin"}
        except Exception as e:  # noqa: BLE001
            print(f"[send] owned-stdin failed {session_id[:8]}: {e}", flush=True)
    else:
        external_live = set(live_sessions()) - set(_sessions.owned_live_ids())
        take_over = session_id not in external_live  # nobody else owns it → just adopt
        if not take_over:
            # Live in another owner (almost always the Claude desktop app). If it's
            # sitting IDLE (not mid-turn), take it over so the phone works: kill that
            # process, then adopt into Ground Control. If it's actively responding,
            # don't corrupt it — fall through to legacy/soft-fail.
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if not is_working(session_id, True, mtime, False, path):
                # Only take over once we've CONFIRMED the external process exited —
                # never risk two processes writing the same transcript. release_session
                # SIGTERMs and returns True only when the pid is gone.
                if release_session(session_id):
                    take_over = True
        if take_over:
            # Dormant (or just-released) → wake it as its OWN EZ terminal, never a
            # headless `--resume` twin. Same session, one live process, the
            # shareable one-stream form. Booting Claude takes a few seconds, so do
            # it off-thread and return immediately; the client polls it in.
            threading.Thread(target=_wake_ez_and_send,
                             args=(session_id, text), daemon=True).start()
            return {"ok": True, "via": "terminal-wake"}
    # Legacy fallback: a live channel delivers straight into the running session —
    # no kill. Only terminal sessions launched with the channel flag have one.
    port = _live_channel_port(session_id)
    if port:
        try:
            import httpx
            r = httpx.post(f"http://127.0.0.1:{port}/push", json={"content": text}, timeout=5)
            if r.status_code == 200:
                return {"ok": True, "via": "channel"}
        except Exception:
            pass  # channel unreachable → fall through
    # Desktop app path: if this session is open/live in the Claude desktop app,
    # type the message into it — the app does the turn natively, no kill/resume.
    if _desktop_app_running():
        title = _session_title(session_id)
        if _type_into_desktop(text, title):
            print(f"[send] {session_id[:8]} -> desktop-type (focused '{title}')", flush=True)
            return {"ok": True, "via": "desktop-type"}
        # Desktop-type failed. If the session is LIVE (running in the app/terminal),
        # NEVER fall to kill-resume — that interrupts it. Soft-fail instead.
        if session_id in live_sessions():
            print(f"[send] {session_id[:8]} '{title}' desktop-type failed + live -> soft-fail (no interrupt)", flush=True)
            return {"ok": False, "via": "blocked",
                    "note": "Couldn't reach the desktop session (its sidebar row wasn't visible). Open it on the Mac, then resend."}
        print(f"[send] {session_id[:8]} '{title}' not live -> kill-resume (safe)", flush=True)
    with _queue_lock:
        _queues.setdefault(session_id, []).append(text)
        depth = len(_queues[session_id])
        start = session_id not in _queue_workers
        if start:
            _queue_workers.add(session_id)
    if start:
        threading.Thread(target=_queue_worker, args=(session_id, cwd), daemon=True).start()
    return {"ok": True, "queued": depth}


# price per 1M tokens: (input, output, cache_read, cache_write_5m)
# price per 1M tokens: (input, output, cache_read, cache_write_5m) — CURRENT list prices.
# opus = Opus 4.8 ($5/$25); the old (15,75,...) here was stale Claude-3-Opus and 3x-overcharged.
_CTX_PRICES = {"fable": (10.0, 50.0, 1.0, 12.5), "opus": (5.0, 25.0, 0.50, 6.25),
               "sonnet": (3.0, 15.0, 0.30, 3.75), "haiku": (1.0, 5.0, 0.10, 1.25)}


def _model_family(model: str) -> str:
    m = (model or "").lower()
    if "fable" in m:
        return "fable"
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return "opus"


def _model_label(raw: str) -> str:
    """'claude-opus-4-8' → 'Opus 4.8'; 'claude-haiku-4-5-20251001' → 'Haiku 4.5'."""
    if not raw:
        return ""
    parts = raw.replace("claude-", "").split("-")
    if not parts:
        return raw
    family = parts[0].capitalize()
    nums = [p for p in parts[1:] if p.isdigit()][:2]
    return f"{family} {'.'.join(nums)}".strip()


def _latest_permission_mode(path: Path):
    for e in reversed(_read_lines(path)):
        pm = e.get("permissionMode")
        if pm:
            return pm
    return None


@app.get("/api/context/{project_dir}/{session_id}")
def context_info(project_dir: str, session_id: str):
    """Per-session context window: how full THIS session's context is, and what
    each message currently costs at that size. Powers the app's /context command."""
    eff = _effective_sid(project_dir, session_id)   # follow /clear rotations
    path = PROJECTS_DIR / project_dir / f"{eff}.jsonl"
    if not path.exists():
        path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    usage, model, model_ts = None, "", 0.0
    for e in reversed(_read_lines(path)):
        msg = e.get("message") or {}
        if msg.get("role") == "assistant" and msg.get("usage"):
            usage, model = msg["usage"], msg.get("model", "")
            model_ts = _entry_ts(e) or 0.0
            break
    perm = _latest_permission_mode(path)
    # A switch we made via /model is newer than anything in the transcript until the
    # next assistant reply lands — otherwise the app shows the pre-switch model.
    label_override = ""
    with _model_override_lock:
        ov = _MODEL_OVERRIDE.get(session_id)
        if ov:
            if model_ts and model_ts > ov[1]:
                _MODEL_OVERRIDE.pop(session_id, None)   # transcript caught up
            else:
                label_override = ov[0]
    if not usage:
        return {"context_tokens": 0, "window": 200000, "pct": 0, "cost": 0,
                "model": model, "model_label": label_override or _model_label(model),
                "permission_mode": perm, "messages": 0, "cost_sonnet": 0}
    inp = usage.get("input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    ctx = inp + cr + cw
    window = 1000000 if ctx > 200000 else 200000
    pi, po, pcr, pcw = _CTX_PRICES[_model_family(model)]
    si, so, scr, scw = _CTX_PRICES["sonnet"]
    cost = inp * pi / 1e6 + cr * pcr / 1e6 + cw * pcw / 1e6 + out * po / 1e6
    cost_sonnet = inp * si / 1e6 + cr * scr / 1e6 + cw * scw / 1e6 + out * so / 1e6
    return {
        "context_tokens": ctx, "window": window,
        "pct": round(ctx / window * 100, 1),
        "messages": cr + cw,        # the conversation history sitting in context
        "output": out, "cost": round(cost, 4), "cost_sonnet": round(cost_sonnet, 4),
        "model": _model_family(model),
        "model_label": label_override or _model_label(model),   # 'Opus 4.8' for the status bar
        "permission_mode": perm,               # 'bypassPermissions', etc.
    }


@app.get("/api/session/{project_dir}/{session_id}/queue")
def queue_depth(project_dir: str, session_id: str):
    with _queue_lock:
        return {"depth": len(_queues.get(session_id) or [])}


# ---------------------------------------------------------------- static

# ---------------------------------------------------------------- editing
# (DESKTOP_CONFIG is defined next to DESKTOP_DIR, with the fresh-Mac scaffold.)


def _find_record_file(sid: str):
    for f in DESKTOP_DIR.glob("claude-code-sessions/*/*/local_*.json"):
        try:
            d = json.load(open(f))
            if d.get("cliSessionId") == sid:
                return f, d
        except (json.JSONDecodeError, OSError):
            pass
    return None, None


def _edit_desktop_config(fn):
    """Read-modify-write the desktop config, safely.

    ⚠️ NEVER write through a failed read. The old version used _desktop_config(),
    which returns {} when the file is momentarily unreadable (the Claude desktop app
    rewriting it at that instant). One drag-to-a-group during that window saved a
    config containing ONLY the new assignment — wiping every group assignment and
    the desktop app's preferences with it (2026-07-22, all of Phil's sessions dumped
    into Ungrouped). Now: retry the read briefly; if a non-empty file still won't
    parse, ABORT the edit instead of clobbering. Plus a rolling backup on every edit.
    """
    _ensure_desktop_scaffold()
    cfg = None
    for _ in range(5):
        try:
            raw = DESKTOP_CONFIG.read_bytes()
            if raw.strip():
                cfg = json.loads(raw)
            else:
                cfg = {}
            break
        except FileNotFoundError:
            cfg = {}
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(0.15)   # mid-write by the desktop app — wait it out
    if cfg is None:
        raise RuntimeError("desktop config unreadable — refusing to clobber it")
    # Rolling backups: last 5 generations, one per edit. The old single one-time
    # backup was made on Jul 5 and never again — useless by the time it was needed.
    if DESKTOP_CONFIG.exists():
        for i in range(4, 0, -1):
            src = DESKTOP_CONFIG.with_suffix(f".json.bak{i}")
            if src.exists():
                src.replace(DESKTOP_CONFIG.with_suffix(f".json.bak{i + 1}"))
        DESKTOP_CONFIG.with_suffix(".json.bak1").write_bytes(DESKTOP_CONFIG.read_bytes())
    cfg.setdefault("preferences", {}).setdefault("epitaxyPrefs", {}).setdefault("dframe-local-slice", {})
    fn(cfg)
    tmp = DESKTOP_CONFIG.with_suffix(".json.tmp")
    json.dump(cfg, open(tmp, "w"), indent=2)
    tmp.replace(DESKTOP_CONFIG)


class RenameBody(BaseModel):
    title: str


@app.post("/api/session/{session_id}/rename")
def rename_session(session_id: str, body: RenameBody):
    f, d = _find_record_file(session_id)
    if not f:
        return JSONResponse({"error": "session not found in desktop records"}, status_code=404)
    d["title"] = body.title.strip()[:100]
    d["titleSource"] = "user"
    json.dump(d, open(f, "w"))
    return {"ok": True}


@app.get("/api/groups")
def get_groups():
    names_path = Path(__file__).parent / "cg_names.json"
    cg_names = json.load(open(names_path)) if names_path.exists() else {"order": [], "names": {}}
    order = cg_names.get("order", [])
    # Return groups in the SAVED order — not raw dict order — so the reorder
    # up/down buttons actually move rows (the list reflects cg["order"]).
    items = sorted(cg_names["names"].items(),
                   key=lambda kv: order.index(kv[1]) if kv[1] in order else len(order))
    return {"groups": [{"id": k, "name": v} for k, v in items]}


class MoveBody(BaseModel):
    group_id: str = ""  # cg-... or "" to ungroup


@app.post("/api/session/{session_id}/move")
def move_session(session_id: str, body: MoveBody):
    f, d = _find_record_file(session_id)
    if not f:
        return JSONResponse({"error": "session not found in desktop records"}, status_code=404)
    local = "code:" + d["sessionId"]

    def fn(cfg):
        sl = cfg["preferences"]["epitaxyPrefs"].setdefault("dframe-local-slice", {})
        assign = sl.setdefault("customGroupAssignments", {})
        order = sl.setdefault("customGroupOrder", {})
        for members in order.values():
            if local in members:
                members.remove(local)
        if body.group_id:
            assign[local] = body.group_id
            order.setdefault(body.group_id, []).insert(0, local)
        else:
            assign.pop(local, None)

    _edit_desktop_config(fn)
    return {"ok": True}


class ReorderBody(BaseModel):
    direction: str  # "up" or "down"


@app.post("/api/session/{session_id}/reorder")
def reorder_session(session_id: str, body: ReorderBody):
    f, d = _find_record_file(session_id)
    if not f:
        return JSONResponse({"error": "session not found"}, status_code=404)
    local = "code:" + d["sessionId"]

    def fn(cfg):
        order = cfg["preferences"]["epitaxyPrefs"].setdefault("dframe-local-slice", {}).setdefault("customGroupOrder", {})
        for members in order.values():
            if local in members:
                i = members.index(local)
                j = i - 1 if body.direction == "up" else i + 1
                if 0 <= j < len(members):
                    members[i], members[j] = members[j], members[i]
                break

    _edit_desktop_config(fn)
    return {"ok": True}


class PlaceBody(BaseModel):
    group_id: str = ""              # cg-... target group, or "" to ungroup
    before_id: str = ""             # app session id to insert BEFORE; "" = append to end


@app.post("/api/session/{session_id}/place")
def place_session(session_id: str, body: PlaceBody):
    """Drag-and-drop placement: move a session into `group_id` (or ungroup it) at
    an exact spot — right before `before_id`, or at the end if that's empty.
    Subsumes /move + /reorder so the Mac sidebar can drag rows across groups AND
    reorder within a group in one call. NOTE: the Ungrouped bucket has no persisted
    order (it sorts by recency), so dropping THERE only ungroups — position is by
    mtime, same as the reorder buttons have always behaved for ungrouped rows."""
    f, d = _find_record_file(session_id)
    if not f:
        return JSONResponse({"error": "session not found"}, status_code=404)
    local = "code:" + d["sessionId"]
    before_local = None
    if body.before_id:
        bf, bd = _find_record_file(body.before_id)
        if bf:
            before_local = "code:" + bd["sessionId"]

    def fn(cfg):
        sl = cfg["preferences"]["epitaxyPrefs"].setdefault("dframe-local-slice", {})
        assign = sl.setdefault("customGroupAssignments", {})
        order = sl.setdefault("customGroupOrder", {})
        # Pull the dragged member out of wherever it currently lives.
        for members in order.values():
            if local in members:
                members.remove(local)
        if body.group_id:
            assign[local] = body.group_id
            members = order.setdefault(body.group_id, [])
            idx = len(members)  # default: append
            if before_local and before_local != local and before_local in members:
                idx = members.index(before_local)
            members.insert(idx, local)
        else:
            assign.pop(local, None)  # ungroup (order not persisted for ungrouped)

    _edit_desktop_config(fn)
    return {"ok": True}


_ARCHIVED_PATH = Path.home() / ".ground-control" / "archived.json"
_archived_lock = threading.Lock()


def _archived_set() -> set:
    """GC's OWN archived list. The desktop app's isArchived flag lives in files
    the desktop app REWRITES at will (same failure class that wiped groups) —
    archived sessions kept resurrecting. This file is ours alone."""
    try:
        return set(json.load(open(_ARCHIVED_PATH)))
    except (OSError, ValueError):
        return set()


def _set_archived(sid: str, archived: bool):
    with _archived_lock:
        s = _archived_set()
        (s.add if archived else s.discard)(sid)
        _ARCHIVED_PATH.parent.mkdir(parents=True, exist_ok=True)
        json.dump(sorted(s), open(_ARCHIVED_PATH, "w"))


@app.post("/api/session/{session_id}/archive")
def archive_session(session_id: str):
    # 1) OUR archived set — survives desktop-config rewrites, and works for
    #    sessions with no desktop record at all (the Ungrouped 404s).
    _set_archived(session_id, True)
    # 2) Archive = done with it: retire the live EZ terminal too. "Live is
    #    truth" kept archived-but-still-running sessions visible forever.
    #    The wrapper is disposable; the transcript/identity stays on disk.
    try:
        ez = ez_name_for(session_id)
        if gc_ez.is_alive(ez):
            gc_ez.kill(ez)
    except Exception:
        pass
    # 3) Best-effort: mark every desktop record too (keeps the desktop app's
    #    own UI consistent). No 404 if none exist — our set is the truth.
    for f in DESKTOP_DIR.glob("claude-code-sessions/*/*/local_*.json"):
        try:
            d = json.load(open(f))
            if d.get("cliSessionId") == session_id:
                d["isArchived"] = True
                json.dump(d, open(f, "w"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"ok": True}


PINS_PATH = Path(__file__).parent / "folder_pins.json"


def _load_pins():
    if PINS_PATH.exists():
        try:
            return json.load(open(PINS_PATH))
        except json.JSONDecodeError:
            pass
    return []


@app.get("/api/folders")
def list_folders(path: str = ""):
    base = Path(path) if path else Path.home()
    dirs, entries = [], []
    try:
        base = base.expanduser().resolve()
        if not base.is_dir():
            return JSONResponse({"error": "not a folder"}, status_code=400)
        for d in base.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                try:
                    mtime = d.stat().st_mtime
                except OSError:
                    mtime = 0
                entries.append({"name": d.name, "mtime": mtime})
        entries.sort(key=lambda e: e["name"].lower())
        dirs = [e["name"] for e in entries]  # back-compat
    except (OSError, PermissionError):
        pass
    parent = str(base.parent) if base != base.parent else None
    pins = [p for p in _load_pins() if os.path.isdir(p)]
    return {"path": str(base), "dirs": dirs, "entries": entries, "parent": parent, "pins": pins}


class FolderPathBody(BaseModel):
    path: str
    name: str = ""


@app.post("/api/folders/new")
def create_folder(body: FolderPathBody):
    parent = Path(body.path).expanduser()
    name = "".join(c for c in body.name if c not in '/\\:').strip()
    if not name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    new = parent / name
    try:
        new.mkdir(parents=False, exist_ok=True)
    except (OSError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "path": str(new)}


@app.post("/api/folders/pin")
def pin_folder(body: FolderPathBody):
    pins = _load_pins()
    p = str(Path(body.path).expanduser())
    if p in pins:
        pins.remove(p)
    else:
        pins.insert(0, p)
    json.dump(pins, open(PINS_PATH, "w"))
    return {"ok": True, "pinned": p in pins}


class NewSessionBody(BaseModel):
    cwd: str = ""
    name: str = ""          # REQUIRED: the human session name → EZ handle + title
    text: str = ""          # optional first message (session just waits if empty)
    ez_name: str = ""       # legacy alias for name
    resume_sid: str = ""    # optional: resume this Claude session instead of a fresh one


def _forge_desktop_record(session_id: str, cwd: str, title: str):
    """Create a desktop-app session record so the new session appears everywhere."""
    dirs = {}
    for f in DESKTOP_DIR.glob("claude-code-sessions/*/*/local_*.json"):
        dirs[f.parent] = dirs.get(f.parent, 0) + 1
    if dirs:
        target = max(dirs, key=dirs.get)
    else:
        # Fresh Mac (no Claude desktop app, no prior records): create our own record
        # dir instead of giving up. Bailing here made EVERY new session invisible to
        # the app for a brand-new user — the sidebar stayed empty forever.
        target = DESKTOP_DIR / "claude-code-sessions" / "ground-control" / "sessions"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    import uuid as _uuid

    local_id = f"local_{_uuid.uuid4()}"
    now_ms = int(time.time() * 1000)
    rec = {
        "sessionId": local_id,
        "cliSessionId": session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": now_ms,
        "lastActivityAt": now_ms,
        "lastFocusedAt": now_ms,
        "title": title,
        "titleSource": "user",
        "isArchived": False,
        "permissionMode": "bypassPermissions",
        "completedTurns": 0,
    }
    path = target / f"{local_id}.json"
    json.dump(rec, open(path, "w"))
    return path


@app.post("/api/new-session")
def new_session(body: NewSessionBody):
    import uuid as _uuid

    # Pick a folder + a NAME. That name becomes BOTH the EZ terminal handle and the
    # session title, so `ez ls`, the app, and the sidebar all show the same thing.
    # The Claude --session-id stays a UUID (Claude requires that on disk) — invisible
    # plumbing mapped to the name via ez_names.json. First message is optional now:
    # a named session can just boot and wait at the prompt for you to talk to it.
    cwd = str(Path(body.cwd).expanduser())
    if not os.path.isdir(cwd):
        return JSONResponse({"error": "folder does not exist"}, status_code=400)
    name = (body.name or body.ez_name or "").strip()
    if not name:
        return JSONResponse({"error": "session name required"}, status_code=400)
    text = body.text.strip()   # optional
    sid = str(_uuid.uuid4())
    # EZ handle = the name, filesystem-safe (spaces kept — `ez "My Name"` works), and
    # deduped so two sessions never collide on one socket.
    ez = "".join(c for c in name if c not in '/\\:').strip() or sid
    _taken = set(_load_ez_names().values()) | set(gc_ez.list_sessions())
    if ez in _taken:
        _base, _i = ez, 2
        while ez in _taken:
            ez = f"{_base} {_i}"
            _i += 1
    _forge_desktop_record(sid, cwd, name)
    _add_owned(sid)  # the app owns sessions it creates — these are the only ones it shows
    _pretrust_folder(cwd)  # kill the trust / external-CLAUDE.md gates before launch
    _set_ez_name(sid, ez)  # map Claude sid -> friendly EZ handle (the name)
    # --name gives Claude its OWN display name too, so `claude --resume`'s picker and
    # the terminal title show the SAME name as `ez ls` / the app — one name everywhere,
    # not a UUID auto-title on the Claude side.
    gc_ez.start(ez, cwd, [CLAUDE_BIN, "--session-id", sid, "--name", name,
                          "--permission-mode", "bypassPermissions"])

    def _first_msg():
        # Claude's boot repaints the screen and can pause on gates a fresh session
        # hits: the "Do you trust this folder?" prompt (NOT skipped by
        # bypassPermissions) and the welcome banner. Watch the PTY: answer the trust
        # prompt with Enter (default = "Yes, I trust"), wait for the real input
        # prompt, THEN send the first message. Resend once if the turn didn't take.
        def _norm(b: bytes) -> str:
            # A TUI positions text with cursor moves, so literal multi-word strings
            # aren't in the raw stream. Strip ANSI + all whitespace, then match.
            s = b.decode("utf-8", "replace")
            s = _re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
            s = _re.sub(r"\x1b\][^\x07]*(\x07|\x1b\\)", "", s)
            return _re.sub(r"\s+", "", s).lower()

        trusted = False
        ready = False
        for _ in range(60):  # ~30s ceiling
            time.sleep(0.5)
            snap = _norm(gc_ez.snapshot(ez, 100, 40))   # EZ socket == the name now
            if not trusted and ("trustthisfolder" in snap or "yesitrust" in snap):
                gc_ez.send_input(ez, "\r")  # belt-and-suspenders if pretrust missed
                trusted = True
                continue
            if "bypasspermissions" in snap or 'try"' in snap or "?forshortcuts" in snap:
                ready = True
                break
        # No first message → the session is booted and waiting at the prompt. Done.
        if not text:
            return
        time.sleep(0.6)
        # Text and Enter as SEPARATE writes. `text + "\r"` in one write reads as a
        # paste to Claude's composer — the trailing \r becomes a newline IN the
        # message instead of submitting it (the message sat unsubmitted in the
        # composer). The normal send path (session send) already does it this way.
        gc_ez.send_input(ez, text)
        time.sleep(0.3)
        before = _norm(gc_ez.snapshot(ez, 100, 40))
        gc_ez.send_input(ez, "\r")
        # Confirm it SUBMITTED by watching the SCREEN, not the transcript — the CLI
        # can defer flushing the transcript until long after the turn starts
        # (measured: the file may not even exist while Claude is already working),
        # so "no transcript yet" is NOT "didn't land". The old transcript check
        # false-negatived and RE-TYPED the whole text → doubled message. A submitted
        # turn changes the screen (echoed turn, spinner, streaming); a swallowed
        # Enter leaves it frozen. Retrying Enter is harmless (Enter on an empty
        # composer is a no-op) — retyping text is never safe, so we never do it.
        for _ in range(6):
            time.sleep(1.5)
            if _norm(gc_ez.snapshot(ez, 100, 40)) != before:
                return
            gc_ez.send_input(ez, "\r")

    threading.Thread(target=_first_msg, daemon=True).start()
    mark_expecting(sid)   # Phil launched + tasked this session → allow its done-alert
    # project dir name for the client to poll
    enc = cwd.replace("/", "-").replace(" ", "-").replace(".", "-").replace("_", "-")
    return {"ok": True, "session_id": sid, "dir": enc}


class NewGroupBody(BaseModel):
    name: str


@app.post("/api/groups/new")
def new_group(body: NewGroupBody):
    import uuid

    gid = "cg-" + str(uuid.uuid4())
    names_path = Path(__file__).parent / "cg_names.json"
    cg_names = json.load(open(names_path)) if names_path.exists() else {"order": [], "names": {}}
    cg_names["names"][gid] = body.name.strip()[:50]
    cg_names["order"].append(body.name.strip()[:50])
    json.dump(cg_names, open(names_path, "w"), indent=2)

    def fn(cfg):
        sl = cfg["preferences"]["epitaxyPrefs"].setdefault("dframe-local-slice", {})
        sl.setdefault("customGroupOrder", {})[gid] = []

    _edit_desktop_config(fn)
    return {"ok": True, "id": gid}


def _load_cg():
    names_path = Path(__file__).parent / "cg_names.json"
    cg = json.load(open(names_path)) if names_path.exists() else {"order": [], "names": {}}
    return names_path, cg


class GroupRenameBody(BaseModel):
    name: str


@app.post("/api/groups/{group_id}/rename")
def rename_group(group_id: str, body: GroupRenameBody):
    names_path, cg = _load_cg()
    old = cg["names"].get(group_id)
    if old is None:
        return JSONResponse({"error": "group not found"}, status_code=404)
    new = body.name.strip()[:50] or old
    cg["names"][group_id] = new
    # `order` is keyed by name, so keep it in sync on rename.
    cg["order"] = [new if n == old else n for n in cg.get("order", [])]
    json.dump(cg, open(names_path, "w"), indent=2)
    return {"ok": True}


class GroupReorderBody(BaseModel):
    direction: str  # "up" | "down"


@app.post("/api/groups/{group_id}/reorder")
def reorder_group(group_id: str, body: GroupReorderBody):
    names_path, cg = _load_cg()
    name = cg["names"].get(group_id)
    if name is None:
        return JSONResponse({"error": "group not found"}, status_code=404)
    order = cg.get("order", [])
    if name not in order:
        order.append(name)
    i = order.index(name)
    j = i - 1 if body.direction == "up" else i + 1
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
        cg["order"] = order
        json.dump(cg, open(names_path, "w"), indent=2)
    return {"ok": True}


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str):
    names_path, cg = _load_cg()
    name = cg["names"].pop(group_id, None)
    if name is None:
        return JSONResponse({"error": "group not found"}, status_code=404)
    cg["order"] = [n for n in cg.get("order", []) if n != name]
    json.dump(cg, open(names_path, "w"), indent=2)

    # Ungroup its sessions + drop the group from the desktop config.
    def fn(cfg):
        sl = cfg["preferences"]["epitaxyPrefs"].setdefault("dframe-local-slice", {})
        sl.get("customGroupOrder", {}).pop(group_id, None)
        assign = sl.get("customGroupAssignments", {})
        for k in list(assign.keys()):
            if assign.get(k) == group_id:
                assign.pop(k, None)

    _edit_desktop_config(fn)
    return {"ok": True}


# ---------------------------------------------------------------- push

# Legacy PWA web-push keys. OPTIONAL: fresh installs don't have them (native apps
# alert via the APNs relay instead) — loading unconditionally crashed the server at
# boot on every new user's Mac. Missing file → feature quietly off.
try:
    VAPID = json.load(open(Path(__file__).parent / "vapid.json"))
except (OSError, json.JSONDecodeError):
    VAPID = {}
SUBS_PATH = Path(__file__).parent / "subscriptions.json"


def _load_subs():
    if SUBS_PATH.exists():
        try:
            return json.load(open(SUBS_PATH))
        except json.JSONDecodeError:
            return []
    return []


def _save_subs(subs):
    json.dump(subs, open(SUBS_PATH, "w"))


@app.get("/api/vapid-public")
def vapid_public():
    if not VAPID:
        return JSONResponse({"error": "web-push not configured"}, status_code=404)
    return {"key": VAPID["public_key"]}


class SubBody(BaseModel):
    subscription: dict


@app.post("/api/subscribe")
def subscribe(body: SubBody):
    subs = _load_subs()
    ep = body.subscription.get("endpoint")
    if ep and not any(s.get("endpoint") == ep for s in subs):
        subs.append(body.subscription)
        _save_subs(subs)
    return {"ok": True, "count": len(subs)}


def send_push(title: str, msg: str, dir_: str = "", sid: str = ""):
    """Send a web-push notification to all subscribed devices. No-op when the
    legacy web-push keys aren't configured (fresh installs — native apps use the
    APNs relay) or pywebpush isn't installed."""
    if not VAPID:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 0

    subs = _load_subs()
    if not subs:
        return 0
    priv = VAPID["private_key"]
    payload = json.dumps({"title": title, "body": msg, "dir": dir_, "id": sid, "tag": sid or None})
    ok, dead = 0, []
    for s in subs:
        try:
            webpush(
                subscription_info=s,
                data=payload,
                vapid_private_key=priv,
                vapid_claims={"sub": "mailto:philipbuonforte@gmail.com"},
                ttl=3600,
            )
            ok += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                dead.append(s.get("endpoint"))
    if dead:
        _save_subs([s for s in subs if s.get("endpoint") not in dead])
    return ok


# ---- APNs (native app push + badges)

APNS_KEY_PATH = Path(__file__).parent / "apns_key.p8"
APNS_CONF_PATH = Path(__file__).parent / "apns.json"  # {key_id, team_id, bundle_id, env}
# Device tokens live OUTSIDE the git repo so a checkout/reset/sync can never wipe
# them (that was one cause of "notifications stopped working"). Migrate any legacy
# in-repo token file on first run.
_GC_STATE = Path.home() / ".ground-control"
_GC_STATE.mkdir(parents=True, exist_ok=True)
APNS_TOKENS_PATH = _GC_STATE / "apns_tokens.json"
_legacy_tokens = Path(__file__).parent / "apns_tokens.json"
if _legacy_tokens.exists() and not APNS_TOKENS_PATH.exists():
    try:
        APNS_TOKENS_PATH.write_text(_legacy_tokens.read_text())
    except Exception:
        pass
_badge = {"count": 0}
_badge_lock = threading.Lock()


def _apns_conf():
    if APNS_CONF_PATH.exists():
        try:
            return json.load(open(APNS_CONF_PATH))
        except json.JSONDecodeError:
            pass
    return None


def _apns_tokens():
    if APNS_TOKENS_PATH.exists():
        try:
            return json.load(open(APNS_TOKENS_PATH))
        except json.JSONDecodeError:
            return []
    return []


def _apns_jwt(conf):
    import jwt  # PyJWT

    key = open(APNS_KEY_PATH).read()
    return jwt.encode(
        {"iss": conf["team_id"], "iat": int(time.time())},
        key,
        algorithm="ES256",
        headers={"kid": conf["key_id"]},
    )


RELAY_URL = "http://165.22.145.29:8132/push"


# Recent fired alerts, for the Mac app to poll and raise NATIVE macOS notifications
# (the Mac app can't receive the iOS APNs push, so it mirrors the same fires locally).
_recent_alerts = []          # [{ts, title, body, sid, dir}], newest last, capped
_recent_alerts_lock = threading.Lock()


def _record_alert(title, body, sid, dir_):
    with _recent_alerts_lock:
        _recent_alerts.append({"ts": time.time(), "title": title, "body": body,
                               "sid": sid, "dir": dir_})
        if len(_recent_alerts) > 100:
            del _recent_alerts[:-100]


_PUSH_COOLDOWN = 20.0   # s — must stay BELOW the 30s minimum repeat-alert interval
_last_push_ts = {}      # sid -> epoch of the last alert actually delivered
_last_push_lock = threading.Lock()


def send_apns(title, body, dir_="", sid="", badge=None):
    """Send a push to every registered device via APNs HTTP/2.

    If this Mac has no APNs signing key (every user except the app owner),
    deliver through the Ground Control relay instead — zero user setup,
    same as any mainstream app's notification server."""
    # PER-SESSION COOLDOWN — the one gate for every alert type and BOTH
    # surfaces (phone push + Mac feed). One session finish can legitimately
    # trip several triggers (stop alert, waiting alert, watchdog) within a
    # couple seconds; Hank got 3 buzzes at once. One buzz per session per
    # cooldown window, whatever the trigger. Repeat alerts (min 30s) clear it.
    if sid:
        _now = time.time()
        with _last_push_lock:
            if _now - _last_push_ts.get(sid, 0) < _PUSH_COOLDOWN:
                print(f"[apns] cooldown: suppressed {sid[:8]} ({title!r})", flush=True)
                return 0
            _last_push_ts[sid] = _now
    # Record for the Mac app's native-notification feed BEFORE the token check —
    # every alert should reach the Mac even when no phone is registered.
    _record_alert(title, body, sid, dir_)
    tokens = _apns_tokens()
    if not tokens:
        return 0
    conf = _apns_conf()
    if not conf or not APNS_KEY_PATH.exists():
        import httpx

        sent = 0
        # Privacy: relay alerts carry NO conversation content — only the
        # session name and a generic line. Full content stays on the user's
        # own devices and tailnet.
        generic = "Tap to view" if body else ""
        dead = []
        # collapse-id: ONE banner per session, always the freshest. Stable per
        # sid (no timestamp — a timestamped id let pushes seconds apart stack,
        # the 3-banners-at-once bug); a new alert replaces the session's old
        # banner instead of piling up. Distinct sessions still get distinct rows.
        collapse = f"gc-{sid or 'alert'}"
        with httpx.Client(timeout=10) as client:
            for tok in tokens:
                try:
                    r = client.post(RELAY_URL, json={
                        "token": tok, "title": title, "body": generic,
                        "dir": dir_, "id": sid, "badge": badge,
                        "collapse": collapse})
                    if r.status_code == 200:
                        j = r.json()
                        if j.get("ok"):
                            sent += 1
                        elif j.get("reason") == "Unregistered":
                            dead.append(tok)   # stale token from an old install
                except Exception:  # noqa: BLE001
                    pass
        if dead:
            try:
                live = [t for t in _apns_tokens() if t not in dead]
                APNS_TOKENS_PATH.write_text(json.dumps(live))
                print(f"[apns] pruned {len(dead)} dead token(s)", flush=True)
            except OSError:
                pass
        return sent
    import httpx

    auth = _apns_jwt(conf)
    aps = {"alert": {"title": title, "body": body}, "sound": "default", "interruption-level": "time-sensitive"}
    if badge is not None:
        aps["badge"] = badge
    payload = {"aps": aps, "dir": dir_, "id": sid}
    headers = {"authorization": f"bearer {auth}",
               "apns-topic": conf["bundle_id"],
               "apns-push-type": "alert"}
    sent, dead = 0, []
    with httpx.Client(http2=True, timeout=10) as client:
        for tok in tokens:
            # TestFlight/App Store tokens are production; Xcode builds are sandbox.
            # Try production first, fall back to sandbox on BadDeviceToken.
            for host in ("api.push.apple.com", "api.sandbox.push.apple.com"):
                try:
                    r = client.post(f"https://{host}/3/device/{tok}", headers=headers, json=payload)
                    if r.status_code == 200:
                        sent += 1
                        break
                    try:
                        reason = r.json().get("reason", "")
                    except Exception:  # noqa: BLE001
                        reason = ""
                    if reason == "BadDeviceToken":
                        continue  # wrong environment — try the other host
                    if r.status_code == 410 or reason == "Unregistered":
                        dead.append(tok)
                    break
                except Exception:  # noqa: BLE001
                    break
    if dead:
        json.dump([t for t in tokens if t not in dead], open(APNS_TOKENS_PATH, "w"))
    return sent


class TokenBody(BaseModel):
    token: str


@app.post("/api/register-apns")
def register_apns(body: TokenBody):
    print(f"[apns] register called, token len={len(body.token or '')}: {(body.token or '')[:20]}", flush=True)
    tokens = _apns_tokens()
    if body.token and body.token not in tokens:
        tokens.append(body.token)
        json.dump(tokens, open(APNS_TOKENS_PATH, "w"))
    return {"ok": True, "count": len(tokens)}


@app.get("/api/test-push")
def test_push():
    """Fire a test alert to every registered device. Verifies the whole push path."""
    n = send_apns("✅ Ground Control AI", "Test alert — notifications are working!", badge=1)
    return {"ok": True, "sent": n, "devices": len(_apns_tokens())}


OPENAI_KEY_PATH = Path(__file__).parent / "openai_key.txt"
_tts_cache = {}


class TTSBody(BaseModel):
    text: str
    voice: str = "nova"


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Voice memo → text (OpenAI Whisper).

    Powers "hold the thought": you start dictating in the app, leave to check a note
    or a site while still talking, come back and send. The recording keeps running in
    the background on the phone; this turns it into text when you stop.
    """
    if not OPENAI_KEY_PATH.exists():
        return JSONResponse({"error": "no OpenAI key configured"}, status_code=400)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty recording"}, status_code=400)
    import httpx

    key = OPENAI_KEY_PATH.read_text().strip()
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": (file.filename or "memo.m4a", data,
                        file.content_type or "audio/m4a")},
        data={"model": "whisper-1"},
        timeout=180,   # a long ramble can take a while to upload + transcribe
    )
    if r.status_code != 200:
        return JSONResponse({"error": f"transcribe failed: {r.status_code} {r.text[:200]}"},
                            status_code=502)
    return {"text": (r.json().get("text") or "").strip()}


@app.post("/api/tts")
def tts(body: TTSBody):
    """Text → natural speech via OpenAI. Returns MP3 bytes."""
    from fastapi import Response

    if not OPENAI_KEY_PATH.exists():
        return JSONResponse({"error": "no OpenAI key configured"}, status_code=400)
    text = body.text.strip()[:4000]
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    cache_key = (hash(text), body.voice)
    if cache_key in _tts_cache:
        return Response(content=_tts_cache[cache_key], media_type="audio/mpeg")
    import httpx

    key = OPENAI_KEY_PATH.read_text().strip()
    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "gpt-4o-mini-tts", "voice": body.voice, "input": text,
              "instructions": "Read naturally and conversationally, at a comfortable pace."},
        timeout=60,
    )
    if r.status_code != 200:
        return JSONResponse({"error": f"TTS failed: {r.status_code} {r.text[:200]}"}, status_code=502)
    if len(_tts_cache) > 40:
        _tts_cache.clear()
    _tts_cache[cache_key] = r.content
    return Response(content=r.content, media_type="audio/mpeg")


# --- Streaming TTS: register texts once, then GET-stream each so playback starts as the
# audio arrives (instead of waiting for the whole MP3), and the app can queue them gaplessly.
import hashlib as _hashlib
_tts_reg = {}   # tid -> (text, voice)


def _tts_id(text: str, voice: str) -> str:
    return _hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:16]


class TTSBatchBody(BaseModel):
    texts: list = []
    voice: str = "nova"


@app.post("/api/tts-batch")
def tts_batch(body: TTSBatchBody):
    """Register a run of messages for streaming playback; returns a stable id per text
    ('' for empties). The app then GET-streams /api/tts-stream/{id} for each, in order."""
    ids = []
    for t in body.texts:
        t = (str(t) if t is not None else "").strip()[:4000]
        if not t:
            ids.append("")
            continue
        tid = _tts_id(t, body.voice)
        _tts_reg[tid] = (t, body.voice)
        ids.append(tid)
    if len(_tts_reg) > 400:                       # bound memory — keep the most recent
        for k in list(_tts_reg)[:-400]:
            _tts_reg.pop(k, None)
    return {"ids": ids}


@app.get("/api/tts-stream/{tid}")
def tts_stream(tid: str):
    """Stream a registered text's speech. Serves cached bytes instantly on replay; otherwise
    proxies OpenAI's audio as it generates so the player starts almost immediately."""
    from fastapi.responses import StreamingResponse, Response
    if not OPENAI_KEY_PATH.exists():
        return JSONResponse({"error": "no OpenAI key configured"}, status_code=400)
    reg = _tts_reg.get(tid)
    if reg is None:
        return JSONResponse({"error": "unknown tts id"}, status_code=404)
    text, voice = reg
    ck = (hash(text), voice)
    if ck in _tts_cache:                          # replay = instant, range-friendly full file
        return Response(content=_tts_cache[ck], media_type="audio/mpeg")
    key = OPENAI_KEY_PATH.read_text().strip()

    def _gen():
        import httpx
        parts = []
        with httpx.stream(
            "POST", "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o-mini-tts", "voice": voice, "input": text,
                  "response_format": "mp3",
                  "instructions": "Read naturally and conversationally, at a comfortable pace."},
            timeout=60,
        ) as r:
            if r.status_code != 200:
                return
            for chunk in r.iter_bytes():
                parts.append(chunk)
                yield chunk
        if parts:                                 # cache the full audio for instant replay
            if len(_tts_cache) > 40:
                _tts_cache.clear()
            _tts_cache[ck] = b"".join(parts)

    return StreamingResponse(_gen(), media_type="audio/mpeg")


class LogBody(BaseModel):
    msg: str


@app.post("/api/log")
def client_log(body: LogBody):
    print(f"[CLIENT] {body.msg}", flush=True)
    return {"ok": True}


@app.post("/api/clear-badge")
def clear_badge():
    with _badge_lock:
        _badge["count"] = 0
    return {"ok": True}


@app.get("/api/unread-count")
def unread_count():
    """iMessage-style badge source: number of sessions still unread. The app
    sets its icon badge to this (NOT to zero on open), so badge and reminders
    clear together — only when you actually open that session."""
    return {"count": len(_load_unreads())}


class NotifyBody(BaseModel):
    title: str = "Pocket Claude"
    message: str = ""
    dir: str = ""
    session_id: str = ""


@app.post("/api/notify")
def notify(body: NotifyBody):
    n = send_push(body.title, body.message, body.dir, body.session_id)
    return {"ok": True, "sent": n}


# ---- debounced alerts: only fire if the session sits idle for ALERT_DELAY

SETTINGS_PATH = Path(__file__).parent / "settings.json"


def _settings():
    if SETTINGS_PATH.exists():
        try:
            return json.load(open(SETTINGS_PATH))
        except json.JSONDecodeError:
            pass
    return {}


def alert_delay() -> int:
    """Seconds of no activity before we buzz the phone (0 = instant)."""
    return int(_settings().get("alert_delay", 60))


def _muted() -> bool:
    return time.time() < float(_settings().get("mute_until", 0))


@app.get("/api/settings")
def get_settings():
    s = _settings()
    remaining = max(0, int(float(s.get("mute_until", 0)) - time.time()))
    return {"alert_delay": int(s.get("alert_delay", 60)),
            "repeat_alert": int(s.get("repeat_alert", 0)),
            "always_alert": bool(s.get("always_alert", False)),
            "call_delay": int(s.get("call_delay", 0)),
            "call_number": s.get("call_number", ""),
            "mute_remaining": remaining}


class SettingsBody(BaseModel):
    alert_delay: int = -1   # -1 = leave unchanged
    repeat_alert: int = -1  # seconds between re-alerts for unread; 0 = never
    always_alert: int = -1  # -1 leave unchanged, 0 off, 1 on — alert every session
    call_delay: int = -1    # seconds a session stays unacknowledged before we CALL Phil; 0 = never
    call_number: str = ""   # Phil's phone (E.164). "" = leave unchanged; "-" = clear


@app.post("/api/settings")
def set_settings(body: SettingsBody):
    s = _settings()
    if body.alert_delay >= 0:
        s["alert_delay"] = max(0, min(3600, body.alert_delay))
    if body.repeat_alert >= 0:
        s["repeat_alert"] = max(0, min(7200, body.repeat_alert))
    if body.always_alert >= 0:
        s["always_alert"] = bool(body.always_alert)
    if body.call_delay >= 0:
        s["call_delay"] = max(0, min(7200, body.call_delay))
    if body.call_number:
        s["call_number"] = "" if body.call_number == "-" else body.call_number.strip()
    json.dump(s, open(SETTINGS_PATH, "w"))
    return {"ok": True, "alert_delay": s.get("alert_delay", 60),
            "repeat_alert": s.get("repeat_alert", 0),
            "always_alert": bool(s.get("always_alert", False)),
            "call_delay": s.get("call_delay", 0),
            "call_number": s.get("call_number", "")}


class MuteBody(BaseModel):
    minutes: int  # 0 = unmute


@app.post("/api/mute")
def set_mute(body: MuteBody):
    s = _settings()
    s["mute_until"] = time.time() + max(0, min(1440, body.minutes)) * 60 if body.minutes > 0 else 0
    json.dump(s, open(SETTINGS_PATH, "w"))
    remaining = max(0, int(float(s["mute_until"]) - time.time()))
    return {"ok": True, "mute_remaining": remaining}
_pending = {}     # session_id -> {fire_at, sig, title, body, dir}
_pending_lock = threading.Lock()
_ALERT_DEBOUNCE = 10  # seconds of CONTINUOUS idle required after the Stop hook before we
                      # actually alert — a session that pauses then resumes on its own
                      # (queued follow-up, hook chain, next step spinning up) must NOT
                      # produce a premature "done". Any activity restarts this clock.


def _tsig(path):
    """Signature of PHIL's last real message. The done-alert's suppression check means
    "did Phil already reply since the stop?" — so it must count ONLY real user messages,
    NOT assistant ones. Counting assistant messages made the alert SELF-SUPPRESS: the
    Stop hook arms the pending, then Claude's own final assistant message flushes to the
    transcript a beat later → signature changes → the instant alert is dropped, leaving
    only Claude's ~60s 'waiting for input' notification (the observed 60s delay). Ignores
    tool-result user rows (no real text) and title/metadata writes."""
    last = None
    count = 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "user" and not d.get("isSidechain") and d.get("uuid") and _msg_text(d):
                    last = d["uuid"]
                    count += 1
    except OSError:
        return None
    return (count, last)


class EventBody(BaseModel):
    title: str = "Pocket Claude"
    event: str = "Stop"
    message: str = ""
    dir: str = ""
    session_id: str = ""
    transcript: str = ""


def _session_display(sid: str):
    """(session title, group name) from the desktop app's records."""
    for r in _desktop_records().values():
        if r.get("cliSessionId") == sid:
            title = (r.get("title") or "").strip()
            group = None
            try:
                cfg = json.load(open(DESKTOP_CONFIG))
                assign = cfg["preferences"]["epitaxyPrefs"]["dframe-local-slice"].get("customGroupAssignments", {})
                cg = assign.get("code:" + r.get("sessionId", ""))
                if cg:
                    names_path = Path(__file__).parent / "cg_names.json"
                    names = json.load(open(names_path)).get("names", {}) if names_path.exists() else {}
                    group = names.get(cg)
            except (json.JSONDecodeError, OSError, KeyError):
                pass
            return title or None, group
    return None, None


@app.post("/api/test-alert")
def test_alert(sid: str = "", dir: str = "", title: str = "Ground Control",
               body: str = "Test alert — click me to open this session."):
    """Fire a REAL alert (Mac banner + phone push) for a session, on demand.

    Testing aid for the alert → click → open-the-session path. The banner carries the
    same dir/id payload a real alert does, so clicking it exercises the real router."""
    n = send_apns(title, body, dir, sid)
    return {"ok": True, "pushed_to_phones": n, "sid": sid}


@app.post("/api/session-event")
def session_event(body: EventBody):
    """A session stopped or asked for input — arm a delayed alert."""
    # Hooks report the live process's CURRENT sid — after /clear that's a rotated id.
    # Canonicalize to GC's stable identity so the engagement gate + tap-to-open work.
    body.session_id = _stable_sid(body.session_id, body.transcript)
    _last_stop[body.session_id] = time.time()
    print(f"[alert] {time.strftime('%H:%M:%S')} armed {body.session_id[:8]} (event={body.event}, delay={alert_delay()}s)", flush=True)
    with _pending_lock:
        _pending[body.session_id] = {
            "fire_at": time.time() + max(alert_delay(), _ALERT_DEBOUNCE),
            "sig": _tsig(body.transcript),
            "transcript": body.transcript,
            "title": body.title,
            "event": body.event,
            "body": body.message,
            "dir": body.dir,
        }
    return {"ok": True, "armed_in": alert_delay()}


class SubagentBody(BaseModel):
    event: str = ""          # "SubagentStart" | "SubagentStop"
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""


@app.post("/api/subagent-event")
def subagent_event(body: SubagentBody):
    # Same canonicalization as session-event: background-job counters must attach
    # to the stable session id, not a /clear-rotated one.
    body.session_id = _stable_sid(body.session_id)
    """A background subagent spawned or finished (Claude Code SubagentStart/Stop
    hooks). Track live ids per session so is_working() reports the session as working
    while a background agent runs even though the main terminal is at an idle prompt."""
    sid = body.session_id
    if not sid:
        return {"ok": False}
    if body.event == "SubagentStart":
        e = _subagents.setdefault(sid, {"count": 0, "ts": 0.0})
        e["count"] += 1
        e["ts"] = time.time()
    elif body.event == "SubagentStop":
        e = _subagents.get(sid)
        if e:
            e["count"] = max(0, e["count"] - 1)   # never negative on a stray Stop
            e["ts"] = time.time()
            if e["count"] == 0:
                _subagents.pop(sid, None)
    n = _subagents.get(sid, {}).get("count", 0)
    print(f"[subagent] {body.event} {sid[:8]} type={body.agent_type} -> {n} live", flush=True)
    return {"ok": True, "live": n}


_quiet_alerted = set()  # session_ids already watchdog-alerted this quiet episode
_SERVER_START = time.time()

# ---- Engagement gate: only alert for sessions Phil is actually WAITING ON --------
# The alert system exists to tell Phil when a session HE tasked needs him. A session
# he's never touched — one running autonomously, or one he abandoned long ago — must
# NEVER buzz him (that was the "FA: Marketing threw an alert and I haven't opened it
# in forever" noise). So every alert (Stop-done, watchdog, waiting-on-prompt) is gated
# on: has Phil interacted with THIS session more recently than our last alert for it?
# Engagement state PERSISTS across server restarts. It used to be memory-only, so
# every deploy/restart wiped it — silently muting every session (the gate saw them
# all as "untouched") until Phil messaged each one again. That was a main cause of
# "notifications stopped working".
_ENGAGEMENT_PATH = Path.home() / ".ground-control" / "engagement.json"


def _load_engagement():
    try:
        d = json.load(open(_ENGAGEMENT_PATH))
        return d.get("expecting", {}), d.get("alerted_since", {})
    except (OSError, json.JSONDecodeError):
        return {}, {}


def _save_engagement():
    try:
        _ENGAGEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        json.dump({"expecting": _expecting, "alerted_since": _alerted_since},
                  open(_ENGAGEMENT_PATH, "w"))
    except OSError:
        pass


_expecting, _alerted_since = _load_engagement()
# sid -> ts of Phil's last direct interaction / ts of the last alert we fired


def mark_expecting(sid: str):
    """Record that Phil just interacted with this session — so it's allowed to alert
    him about the result. Called from every send path (chat send, terminal type,
    answer, new-session, and an Enter typed into the live terminal). Interacting also
    ACKNOWLEDGES any outstanding alert (he's clearly seen it) → clears unread so repeats
    stop. 'Read' = an active action (reply / open / tap the alert), NOT a screen merely
    being open — that passive-clear (from polling get_session) is what killed repeats."""
    if sid:
        _expecting[sid] = time.time()
        _save_engagement()
        clear_unread(sid, reason="mark_expecting")


def _always_alert() -> bool:
    """Settings toggle: alert for EVERY session, even ones Phil never interacted with
    (bypasses the engagement gate below). Off by default so autonomous sessions stay
    quiet; on = buzz me for everything."""
    return bool(_settings().get("always_alert", False))


def _phil_awaiting(sid: str) -> bool:
    """True iff Phil interacted with this session AFTER our last alert for it — i.e.
    he's waiting on this specific result. Alive-but-autonomous / abandoned sessions
    return False and stay silent — UNLESS the 'always alert' setting is on."""
    if _always_alert():
        return True
    return _expecting.get(sid, 0) > _alerted_since.get(sid, 0)


def _mark_alerted(sid: str):
    _alerted_since[sid] = time.time()
    _save_engagement()


def _watchdog_check():
    """Catch sessions that died mid-turn (crash, credit cutoff, interrupt) —
    they never fire a Stop hook, so without this the user sits in limbo."""
    now = time.time()
    idx = _transcript_index()
    for sid, pid in live_sessions().items():
        path = idx.get(sid)
        if path is None:
            continue
        # Re-arm: if the session is genuinely WORKING again (reliable terminal read),
        # forget the past alert so a FUTURE quiet episode can alert once. This is what
        # stops the repeat firing — the old dedup was by mtime, and metadata writes
        # (ai-title / snapshots) bump mtime and re-triggered it endlessly.
        ez = ez_name_for(sid)
        if gc_ez.is_alive(ez) and gc_ez.is_working(ez) is True:
            _quiet_alerted.discard(sid)
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < _SERVER_START:
            continue  # no activity observed this run — not our watch
        quiet_for = now - mtime
        if not (90 <= quiet_for <= 600):
            continue  # still working, or ancient
        if _last_stop.get(sid, 0) >= mtime - 5:
            continue  # a real Stop hook covered this activity — normal flow
        if not _phil_awaiting(sid):
            continue  # Phil never engaged this session → not his concern, stay silent
        if sid in _quiet_alerted:
            continue  # already alerted for this quiet episode (re-arms only on real work)
        if _alerted_since.get(sid, 0) >= mtime:
            continue  # PERSISTED dedupe: already alerted for this activity — survives
                      # server restarts (the in-memory set above doesn't, and restart
                      # loops re-buzzed the same quiet sessions over and over)
        with _pending_lock:
            if sid in _pending:
                continue
        _quiet_alerted.add(sid)
        _mark_alerted(sid)
        if _muted():
            mark_unread(sid)
            print(f"[alert] muted (watchdog) {sid[:8]}", flush=True)
            continue
        stitle, proj = _session_display(sid)
        title = f"⚠️ {stitle or 'Session'}" + (f" · {proj}" if proj else "")
        n_web = send_push(title, "Went quiet without finishing — may need you", path.parent.name, sid)
        n_apns = send_apns(title, "Went quiet without finishing — may need you", path.parent.name, sid,
                           badge=mark_unread(sid))
        print(f"[alert] {time.strftime('%H:%M:%S')} WATCHDOG {sid[:8]} -> web:{n_web} apns:{n_apns}: {title}", flush=True)


_last_alerted = {}  # session_id -> epoch of last push we sent for it


def _repeat_check():
    """Re-buzz for sessions still unread after the configured repeat interval."""
    repeat = int(_settings().get("repeat_alert", 0))
    if repeat <= 0 or _muted():
        return
    now = time.time()
    unreads = _load_unreads()
    for sid, marked_ts in unreads.items():
        last = _last_alerted.get(sid, float(marked_ts))
        if now - last < repeat:
            continue
        _last_alerted[sid] = now
        stitle, proj = _session_display(sid)
        title = f"🔁 {stitle or 'Session'}" + (f" · {proj}" if proj else "")
        idx = _transcript_index()
        dir_ = idx[sid].parent.name if sid in idx else ""
        n_web = send_push(title, "Still waiting on you", dir_, sid)
        n_apns = send_apns(title, "Still waiting on you", dir_, sid, badge=len(unreads))
        print(f"[alert] REPEAT {sid[:8]} -> web:{n_web} apns:{n_apns}", flush=True)


_waiting_alerted = {}  # session_id -> prompt label already alerted (cleared when the prompt clears)


def _waiting_check():
    """THE app's #1 alert: the moment a session BLOCKS on an interactive prompt
    (question / permission / trust), buzz Phil immediately so he's never stuck
    waiting on Claude. Fire once per prompt; re-arm when it clears."""
    if _muted():
        return
    names = gc_ez.list_sessions()
    if not names:
        return
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
        results = list(ex.map(lambda n: (n, gc_ez.waiting_for_input(n)), names))
    kinds = {"question": "a question", "permission": "a permission prompt",
             "trust": "folder trust", "prompt": "your input"}
    for name, label in results:
        sid = sid_for_ez(name) or name
        if not label:
            _waiting_alerted.pop(sid, None)   # prompt gone → allow a fresh alert next time
            continue
        if _waiting_alerted.get(sid) == label:
            continue                          # already alerted for this exact prompt
        if not _phil_awaiting(sid):
            continue  # a session Phil never engaged blocking on a prompt is not his concern
        _waiting_alerted[sid] = label
        _mark_alerted(sid)
        stitle, proj = _session_display(sid)
        title = f"⏸ {stitle or 'Session'}" + (f" · {proj}" if proj else "")
        idx = _transcript_index()
        dir_ = idx[sid].parent.name if sid in idx else ""
        msg = f"Waiting on {kinds.get(label, 'your input')} — needs you now"
        n_web = send_push(title, msg, dir_, sid)
        n_apns = send_apns(title, msg, dir_, sid, badge=mark_unread(sid))
        print(f"[alert] WAITING {sid[:8]} ({label}) -> web:{n_web} apns:{n_apns}", flush=True)


def _reap_owned_twins():
    """Enforce the single-brain invariant: a session with a live EZ terminal must
    NOT also have an owned-stdin `claude --resume` process. Two processes on one
    transcript diverge — that's the terminal/chat "not in sync" bug. The terminal
    is always the source of truth, so any owned twin gets killed. This is the
    airtight backstop: it reconciles every 5s no matter which code path (chat
    cold-start, takeover, race) created the twin."""
    for sid in list(_sessions.owned_live_ids()):
        if gc_ez.is_alive(ez_name_for(sid)):
            _sessions.stop(sid)
            print(f"[reap] killed owned twin for {sid[:8]} — terminal is the brain", flush=True)


def _terminal_work_warmer():
    """Keep the terminal working-state cache warm so every session-list row and
    cold busy-check reflects the real terminal (esc-to-interrupt line) within ~1.5s
    without blocking any request. Terminal is the brain."""
    while True:
        try:
            names = gc_ez.list_sessions()
            gc_ez.refresh_working(names)
            # Pre-render the status LABEL here, off the request path. work_label caches
            # the render; doing it in this background thread means /api/work is a pure
            # cache read (~5ms) instead of a 0.4s socket read against the same EZ daemon
            # that streams the live terminal — that inline render made /api/work ~545ms
            # every poll and contended with typing.
            for n in names:
                try:
                    gc_ez.work_label(n)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"[work-warm] {e}", flush=True)
        time.sleep(0.8)


@app.get("/api/mac-alerts")
def mac_alerts(since: float = 0.0):
    """Alerts fired since `since` (epoch), for the Mac app to raise as native macOS
    notifications — it can't receive the iOS APNs push, so it polls this and mirrors
    the same fires locally. Returns {now, alerts:[...]}. First call (since=0) returns
    `now` only (no backlog spam), so the app arms from 'now' forward."""
    now = time.time()
    if since <= 0:
        return {"now": now, "alerts": []}
    with _recent_alerts_lock:
        fresh = [a for a in _recent_alerts if a["ts"] > since]
    return {"now": now, "alerts": fresh}


@app.get("/api/dismissed")
def dismissed(since: float = 0.0):
    """Sessions acknowledged since `since` (epoch), so the OTHER device can pull their
    leftover notification banners (iMessage-style: dismiss once, gone everywhere). First
    call (since=0) returns `now` only, so a client arms from 'now' forward."""
    now = time.time()
    if since <= 0:
        return {"now": now, "sids": []}
    with _dismissed_lock:
        sids = list({d["sid"] for d in _dismissed if d["ts"] > since})
    return {"now": now, "sids": sids}


_BLAND_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/125 Safari/537.36")


def _e164(num: str) -> str:
    """Normalize a phone number to E.164 (Bland requires it). A US 10-digit number
    typed without a country code (e.g. '8014738272') becomes '+18014738272'."""
    num = (num or "").strip()
    digits = "".join(ch for ch in num if ch.isdigit())
    if num.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def _bland_conf() -> dict:
    """Bland secrets from a GITIGNORED bland.json (bland_key, bland_relay_secret,
    optional bland_from/bland_voice). Kept out of the tracked settings.json."""
    p = Path(__file__).parent / "bland.json"
    if p.exists():
        try:
            return json.load(open(p))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _place_call(number: str, message: str) -> bool:
    """Phone-call Phil and speak `message`, then hang up. Uses the SAME Bland.ai setup as
    the Link-X Sales Agent (from-number +14158775842). Config from settings.json:
      bland_key            — Bland API key (required)
      bland_relay_secret   — if set, route via the Cloudflare relay (needed only from
                             DigitalOcean IPs; from Phil's Mac we can hit Bland directly)
      bland_from / bland_voice — optional overrides
    Returns True if Bland accepted the call."""
    # Bland secrets live in a GITIGNORED bland.json (NOT settings.json, which is tracked),
    # falling back to env. Non-secret overrides (from/voice) may come from settings.json.
    bc = _bland_conf()
    s = _settings()
    key = bc.get("bland_key") or s.get("bland_key") or os.environ.get("BLAND_API_KEY", "")
    if not key:
        print(f"[call] no bland_key configured — WOULD call {number}: {message}", flush=True)
        return False
    relay_secret = bc.get("bland_relay_secret") or s.get("bland_relay_secret") or os.environ.get("BLAND_RELAY_SECRET", "")
    payload = {
        "phone_number": _e164(number),
        "from": bc.get("bland_from") or s.get("bland_from", "+14158775842"),
        "task": (f"You are Ground Control's alert bot. The instant the person answers say "
                 f"exactly: \"{message}\" Then say goodbye and end the call. Do NOT ask "
                 f"questions or hold a conversation."),
        "voice": s.get("bland_voice", "Allie"),
        "model": "base",
        "wait_for_greeting": True,
        "max_duration": 2,
    }
    headers = {"authorization": key, "Content-Type": "application/json"}
    if relay_secret:
        url = "https://bland-relay.phil-838.workers.dev/v1/calls"
        headers["x-relay-secret"] = relay_secret
        headers["User-Agent"] = _BLAND_UA
    else:
        url = "https://api.bland.ai/v1/calls"   # direct — fine from Phil's Mac (not a DO IP)
    try:
        import httpx
        r = httpx.post(url, headers=headers, json=payload, timeout=20)
        ok = r.status_code in (200, 201)
        print(f"[call] bland {number} -> {r.status_code} {r.text[:160]}", flush=True)
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[call] error calling {number}: {e}", flush=True)
        return False


def _call_check():
    """Escalate to a PHONE CALL when Phil has left a session unacknowledged for the
    configured `call_delay`. One call per unread episode (cleared when he opens/replies
    → clear_unread discards it). Same engagement/mute gates as push alerts."""
    call_delay = int(_settings().get("call_delay", 0))
    number = _settings().get("call_number", "")
    if call_delay <= 0 or not number or _muted():
        return
    now = time.time()
    for sid, marked_ts in list(_load_unreads().items()):
        if now - float(marked_ts) < call_delay:
            continue
        if sid in _called:
            continue
        # NOTE: no _phil_awaiting gate here. Being UNREAD already means an alert fired,
        # which ALREADY passed the engagement gate. Re-checking _phil_awaiting always
        # failed because the alert itself calls _mark_alerted → _alerted_since >=
        # _expecting → _phil_awaiting False → the call was silently skipped forever.
        _called.add(sid)
        title, _ = _session_display(sid)
        _place_call(number, f"Ground Control. {title or 'A session'} needs you. Open the app.")
        print(f"[alert] {time.strftime('%H:%M:%S')} CALL {sid[:8]} -> {number} (unread {int(now-float(marked_ts))}s)", flush=True)


_GROUPS_SHADOW = Path.home() / ".ground-control" / "groups_shadow.json"


def _groups_guard():
    """Self-healing shadow for group assignments. The Claude desktop app
    periodically rewrites its config and DROPS our customGroupAssignments/
    customGroupOrder keys (it wiped Phil's groups twice in two days). Snapshot
    them to GC's own file whenever they're non-empty; if they ever vanish while
    the shadow has them, write them straight back."""
    cfg = _desktop_config()
    sl = ((cfg.get("preferences") or {}).get("epitaxyPrefs") or {}).get("dframe-local-slice") or {}
    assign = sl.get("customGroupAssignments") or {}
    order = sl.get("customGroupOrder") or {}
    if assign:
        # live state exists → keep the shadow current
        snap = {"customGroupAssignments": assign, "customGroupOrder": order}
        try:
            old = json.load(open(_GROUPS_SHADOW)) if _GROUPS_SHADOW.exists() else None
        except (OSError, json.JSONDecodeError):
            old = None
        if old != snap:
            try:
                _GROUPS_SHADOW.parent.mkdir(parents=True, exist_ok=True)
                json.dump(snap, open(_GROUPS_SHADOW, "w"))
            except OSError:
                pass
        return
    # assignments GONE — restore from shadow if we have one
    try:
        snap = json.load(open(_GROUPS_SHADOW))
    except (OSError, json.JSONDecodeError):
        return
    if not snap.get("customGroupAssignments"):
        return

    def fn(c):
        s = c.setdefault("preferences", {}).setdefault("epitaxyPrefs", {}).setdefault("dframe-local-slice", {})
        if not s.get("customGroupAssignments"):
            s["customGroupAssignments"] = snap["customGroupAssignments"]
            s.setdefault("customGroupOrder", snap.get("customGroupOrder", {}))

    _edit_desktop_config(fn)
    print(f"[groups] desktop app wiped group assignments — restored "
          f"{len(snap['customGroupAssignments'])} from shadow", flush=True)


def _alert_worker():
    while True:
        time.sleep(2)   # 2s (was 5s) so a truly-idle / needs-you alert fires within ~2s
        try:
            _reap_owned_twins()
            _watchdog_check()
            _waiting_check()
            _repeat_check()
            _call_check()
            _groups_guard()
        except Exception as e:  # noqa: BLE001
            print(f"[alert] watchdog error: {e}", flush=True)
        now = time.time()
        due = []
        with _pending_lock:
            for sid, p in list(_pending.items()):
                # DEBOUNCE / continuous-idle: don't fire the instant the hook lands — a
                # session often pauses ~2s then resumes on its own (Claude keeps working),
                # which produced "got an alert but it's still running" false alarms. Every
                # tick, if the session is working (terminal OR a background subagent), push
                # the fire time out by the full debounce. So the alert fires ONLY after the
                # session has been continuously idle for _ALERT_DEBOUNCE seconds; any activity
                # restarts the clock. ("Need you" prompts still fire immediately via _waiting_check.)
                ez = ez_name_for(sid)
                working = (gc_ez.is_alive(ez) and gc_ez.is_working(ez, allow_snapshot=True) is True) \
                    or _subagent_running(sid)
                if not working:
                    # Transcript progress counts as work too: a continuation that
                    # hasn't painted the terminal yet still WRITES the transcript
                    # (tool records, assistant turns). Quiet terminal + fresh writes
                    # = not done — this was the premature-"done" blind spot.
                    tp = p.get("transcript")
                    if tp:
                        try:
                            if now - os.stat(tp).st_mtime < 8:
                                working = True
                        except OSError:
                            pass
                if working:
                    p["fire_at"] = now + _ALERT_DEBOUNCE
                    continue
                if now >= p["fire_at"]:
                    due.append((sid, p))
                    del _pending[sid]
        for sid, p in due:
            # (working already confirmed idle for the full debounce window above)
            # if a new turn landed since the stop, you responded → skip
            now_sig = _tsig(p["transcript"])
            if now_sig != p["sig"]:
                print(f"[alert] {time.strftime('%H:%M:%S')} suppressed {sid[:8]} (activity {p['sig']} -> {now_sig})", flush=True)
                continue
            # Only buzz Phil if he's actually waiting on this session (he sent to it
            # since the last alert). Autonomous / untouched sessions stay silent.
            if not _phil_awaiting(sid):
                print(f"[alert] skipped {sid[:8]} (Phil not awaiting — untouched/autonomous)", flush=True)
                continue
            _mark_alerted(sid)
            stitle, proj = _session_display(sid)
            emoji = "⏳" if p.get("event") == "Notification" else "✅"
            if stitle:
                title = f"{emoji} {stitle}" + (f" · {proj}" if proj else "")
            else:
                title = p["title"]
            badge = mark_unread(sid)
            if _muted():
                print(f"[alert] muted {sid[:8]}: {title}", flush=True)
                continue
            n_web = send_push(title, p["body"], p["dir"], sid)
            n_apns = send_apns(title, p["body"], p["dir"], sid, badge=badge)
            print(f"[alert] {time.strftime('%H:%M:%S')} FIRED {sid[:8]} ({p.get('event')}) -> web:{n_web} apns:{n_apns} badge:{badge}: {title}", flush=True)


_img_cache = {}  # cache_key -> (bytes, media_type)
_MAX_IMG_DIM = 1200


def _downscale(data: bytes, cache_key):
    """Resize an image to phone size (huge screenshots were freezing the app)."""
    if cache_key in _img_cache:
        return _img_cache[cache_key]
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.thumbnail((_MAX_IMG_DIM, _MAX_IMG_DIM))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        out = (buf.getvalue(), "image/jpeg")
    except Exception:  # noqa: BLE001 — serve original if resize fails
        out = (data, "application/octet-stream")
    if len(_img_cache) > 300:
        _img_cache.clear()
    _img_cache[cache_key] = out
    return out


@app.get("/api/msgimg/{project_dir}/{session_id}/{uuid}/{idx}")
def serve_msg_image(project_dir: str, session_id: str, uuid: str, idx: str):
    """Serve an image embedded inside a transcript message. `idx` is either "i"
    (top-level image block) or "i-j" (image nested in the content of the i-th
    tool_result — e.g. a screenshot the agent Read)."""
    import base64

    from fastapi import Response

    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        if "-" in idx:
            i, j = (int(x) for x in idx.split("-", 1))
        else:
            i, j = int(idx), None
    except ValueError:
        return JSONResponse({"error": "bad index"}, status_code=400)
    for e in _read_lines(path):
        if e.get("uuid") == uuid:
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and 0 <= i < len(content):
                b = content[i]
                if j is not None and isinstance(b, dict) and b.get("type") == "tool_result":
                    inner = b.get("content")
                    b = inner[j] if isinstance(inner, list) and 0 <= j < len(inner) else None
                if isinstance(b, dict) and b.get("type") == "image":
                    src = b.get("source", {})
                    if src.get("type") == "base64":
                        data = base64.b64decode(src.get("data", ""))
                        body, mt = _downscale(data, ("msg", uuid, idx))
                        return Response(content=body, media_type=mt,
                                        headers={"Cache-Control": "max-age=86400"})
            break
    return JSONResponse({"error": "no image"}, status_code=404)


@app.get("/api/file")
def serve_file(path: str):
    from fastapi import Response

    p = Path(path)
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return JSONResponse({"error": "not an image"}, status_code=400)
    if not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    st = p.stat()
    body, mt = _downscale(p.read_bytes(), ("file", str(p), st.st_mtime))
    return Response(content=body, media_type=mt,
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/icon-192.png")
def icon192():
    return FileResponse(STATIC_DIR / "icon-192.png")


@app.get("/icon-512.png")
def icon512():
    return FileResponse(STATIC_DIR / "icon-512.png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
