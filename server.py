"""Pocket Claude — phone dashboard for Claude Code sessions on this Mac.

Lists all sessions from the shared session store, shows conversations,
and injects messages via `claude -p --resume` (auto-releasing any live
desktop-app process first so history never forks).
"""
import glob
import json
import os
import shutil
import signal
import urllib.parse
import subprocess
import threading
import time
from pathlib import Path

import asyncio

from fastapi import FastAPI, Request, WebSocket
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
              Path("/usr/local/bin/claude")):
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


def parse_turns(path: Path):
    """Return conversation turns following the active branch (last leaf → root)."""
    lines = _read_lines(path)
    nodes = {}
    leaf = None
    for e in lines:
        if e.get("uuid"):
            nodes[e["uuid"]] = e
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
            clean, images = _extract_images(text)
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


_balance_cache = {"ts": 0.0, "val": None}


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
    Anthropic dict, or {'error': ...}."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            text=True, timeout=5,
        ).strip()
        tok = json.loads(raw)["claudeAiOauth"]["accessToken"]
    except Exception:  # noqa: BLE001
        return {"error": "no_token"}
    import httpx

    try:
        r = httpx.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"},
            timeout=15,
        )
        if r.status_code != 200:
            return {"error": f"api_{r.status_code}"}
        return r.json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


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
    gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", sid, *_name_flag(name),
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
    gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", sid, *_name_flag(name),
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

    def build_session(local_id):
        r = recs.get(local_id)
        if not r or r.get("isArchived"):
            return None
        sid = r.get("cliSessionId")
        if sid not in owned:
            return None  # app only shows sessions born in it, not the desktop-app world
        path = idx.get(sid)
        if path is None:
            return None
        try:
            _, preview, _ = session_meta(path)
            mtime = path.stat().st_mtime
        except OSError:
            return None
        with _jobs_lock:
            job = _jobs.get(sid, {})
        return {
            "id": sid,
            "ezName": ezmap.get(sid, sid),
            "resumeName": sid,
            "dir": path.parent.name,
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
    groups = [g for g in groups if g["sessions"]]
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
def get_session(project_dir: str, session_id: str):
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
            waiting_question = _pending_question(path) or gc_ez.terminal_question(ez)
    return {
        "turns": turns[-80:],
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
        gc_ez.start(name, cwd, [CLAUDE_BIN, "--resume", session_id, *_name_flag(name),
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


@app.post("/api/session/{project_dir}/{session_id}/send")
def send_message(project_dir: str, session_id: str, body: SendBody):
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    text = _build_text(body.text, body.attachments)
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
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    usage, model = None, ""
    for e in reversed(_read_lines(path)):
        msg = e.get("message") or {}
        if msg.get("role") == "assistant" and msg.get("usage"):
            usage, model = msg["usage"], msg.get("model", "")
            break
    perm = _latest_permission_mode(path)
    if not usage:
        return {"context_tokens": 0, "window": 200000, "pct": 0, "cost": 0,
                "model": model, "model_label": _model_label(model),
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
        "model_label": _model_label(model),   # 'Opus 4.8' for the status bar
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
    """Read-modify-write the desktop config with a one-time backup. Resilient on a
    fresh Mac: the scaffold guarantees the file, and the edit closure always gets
    the nested structure it expects even if the file was empty/corrupt."""
    _ensure_desktop_scaffold()
    if DESKTOP_CONFIG.exists():
        bak = DESKTOP_CONFIG.with_suffix(".json.pocketclaude-bak")
        if not bak.exists():
            bak.write_bytes(DESKTOP_CONFIG.read_bytes())
    cfg = _desktop_config()
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


@app.post("/api/session/{session_id}/archive")
def archive_session(session_id: str):
    f, d = _find_record_file(session_id)
    if not f:
        return JSONResponse({"error": "session not found"}, status_code=404)
    d["isArchived"] = True
    json.dump(d, open(f, "w"))
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


def send_apns(title, body, dir_="", sid="", badge=None):
    """Send a push to every registered device via APNs HTTP/2.

    If this Mac has no APNs signing key (every user except the app owner),
    deliver through the Ground Control relay instead — zero user setup,
    same as any mainstream app's notification server."""
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
        with httpx.Client(timeout=10) as client:
            for tok in tokens:
                try:
                    r = client.post(RELAY_URL, json={
                        "token": tok, "title": title, "body": generic,
                        "dir": dir_, "id": sid, "badge": badge})
                    if r.status_code == 200:
                        sent += 1
                except Exception:  # noqa: BLE001
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
_ALERT_DEBOUNCE = 5   # seconds of CONTINUOUS idle required after the Stop hook before we
                      # actually alert — a session that pauses ~2s then resumes on its own
                      # must NOT produce an alert. Any activity restarts this clock.


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
_expecting = {}       # sid -> ts of Phil's last direct interaction (send / type / answer / terminal Enter)
_alerted_since = {}   # sid -> ts of the last alert we fired for this session


def mark_expecting(sid: str):
    """Record that Phil just interacted with this session — so it's allowed to alert
    him about the result. Called from every send path (chat send, terminal type,
    answer, new-session, and an Enter typed into the live terminal). Interacting also
    ACKNOWLEDGES any outstanding alert (he's clearly seen it) → clears unread so repeats
    stop. 'Read' = an active action (reply / open / tap the alert), NOT a screen merely
    being open — that passive-clear (from polling get_session) is what killed repeats."""
    if sid:
        _expecting[sid] = time.time()
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
        print(f"[alert] WATCHDOG {sid[:8]} -> web:{n_web} apns:{n_apns}: {title}", flush=True)


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


def _alert_worker():
    while True:
        time.sleep(2)   # 2s (was 5s) so a truly-idle / needs-you alert fires within ~2s
        try:
            _reap_owned_twins()
            _watchdog_check()
            _waiting_check()
            _repeat_check()
            _call_check()
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
