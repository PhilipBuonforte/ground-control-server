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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

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


@app.on_event("startup")
def _startup():
    threading.Thread(target=_alert_worker, daemon=True).start()

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
            content = (cur.get("message") or {}).get("content")
            if isinstance(content, list):
                for i, b in enumerate(content):
                    if isinstance(b, dict) and b.get("type") == "image":
                        images.append(f"/api/msgimg/{dir_name}/{sid}/{cur['uuid']}/{i}")
            if clean or images:
                if len(clean) > 2500:
                    clean = clean[:2500] + "\n…[truncated]"
                turns.append(
                    {
                        "id": cur["uuid"],
                        "role": cur["type"],
                        "text": clean,
                        "images": images,
                        "ts": cur.get("timestamp"),
                    }
                )
        cur = nodes.get(cur.get("parentUuid"))
    turns.reverse()
    return turns


# When a turn ends, the Stop hook records the timestamp here. A live session is
# "working" only if its transcript changed AFTER the last turn ended — this is
# robust across long quiet gaps (thinking, slow tool calls) that a simple
# recency window would misread as idle.
_last_stop = {}  # session_id -> epoch seconds of last Stop/Notification


def is_working(sid: str, live: bool, mtime: float, job_running: bool) -> bool:
    if job_running:
        return True
    if not live:
        return False
    ls = _last_stop.get(sid)
    if ls is not None:
        return mtime > ls + 1.5
    return time.time() - mtime < 30  # fallback until we've seen a Stop this run


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


def clear_unread(session_id: str):
    with _unreads_lock:
        u = _load_unreads()
        if session_id in u:
            del u[session_id]
            json.dump(u, open(UNREADS_PATH, "w"))


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


DESKTOP_DIR = Path.home() / "Library" / "Application Support" / "Claude"


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

    cfg = json.load(open(DESKTOP_DIR / "claude_desktop_config.json"))
    sl = cfg["preferences"]["epitaxyPrefs"]["dframe-local-slice"]
    assign = sl.get("customGroupAssignments", {})
    group_order = sl.get("customGroupOrder", {})

    names_path = Path(__file__).parent / "cg_names.json"
    cg_names = {"order": [], "names": {}}
    if names_path.exists():
        try:
            cg_names = json.load(open(names_path))
        except json.JSONDecodeError:
            pass

    def build_session(local_id):
        r = recs.get(local_id)
        if not r or r.get("isArchived"):
            return None
        sid = r.get("cliSessionId")
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
            "dir": path.parent.name,
            "title": (r.get("title") or "Untitled")[:80],
            "preview": (preview or "")[:120],
            "project": Path(r.get("cwd") or "").name,
            "mtime": mtime,
            "live": sid in live,
            "busy": is_working(sid, sid in live, mtime, job.get("status") == "running"),
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
    return {"groups": groups}


@app.get("/api/session/{project_dir}/{session_id}")
def get_session(project_dir: str, session_id: str):
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    clear_unread(session_id)
    turns = parse_turns(path)
    with _jobs_lock:
        job = _jobs.get(session_id, {})
    mtime = path.stat().st_mtime
    live = session_id in live_sessions()
    busy = is_working(session_id, live, mtime, job.get("status") == "running")
    return {
        "turns": turns[-80:],
        "live": live,
        "busy": busy,
        "work": _work_progress(path) if busy else None,
        "job": {k: job.get(k) for k in ("status", "error")},
        "mtime": mtime,
    }


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


@app.post("/api/session/{project_dir}/{session_id}/send")
def send_message(project_dir: str, session_id: str, body: SendBody):
    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    _, _, cwd = session_meta(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    text = _build_text(body.text, body.attachments)
    with _queue_lock:
        _queues.setdefault(session_id, []).append(text)
        depth = len(_queues[session_id])
        start = session_id not in _queue_workers
        if start:
            _queue_workers.add(session_id)
    if start:
        threading.Thread(target=_queue_worker, args=(session_id, cwd), daemon=True).start()
    return {"ok": True, "queued": depth}


@app.get("/api/session/{project_dir}/{session_id}/queue")
def queue_depth(project_dir: str, session_id: str):
    with _queue_lock:
        return {"depth": len(_queues.get(session_id) or [])}


# ---------------------------------------------------------------- static

# ---------------------------------------------------------------- editing

DESKTOP_CONFIG = DESKTOP_DIR / "claude_desktop_config.json"


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
    """Read-modify-write the desktop config with a one-time backup."""
    bak = DESKTOP_CONFIG.with_suffix(".json.pocketclaude-bak")
    if not bak.exists():
        bak.write_bytes(DESKTOP_CONFIG.read_bytes())
    cfg = json.load(open(DESKTOP_CONFIG))
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
    return {"groups": [{"id": k, "name": v} for k, v in cg_names["names"].items()]}


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
    cwd: str
    text: str


def _forge_desktop_record(session_id: str, cwd: str, title: str):
    """Create a desktop-app session record so the new session appears everywhere."""
    dirs = {}
    for f in DESKTOP_DIR.glob("claude-code-sessions/*/*/local_*.json"):
        dirs[f.parent] = dirs.get(f.parent, 0) + 1
    if not dirs:
        return None
    target = max(dirs, key=dirs.get)
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

    cwd = str(Path(body.cwd).expanduser())
    if not os.path.isdir(cwd):
        return JSONResponse({"error": "folder does not exist"}, status_code=400)
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "message required"}, status_code=400)
    sid = str(_uuid.uuid4())
    _forge_desktop_record(sid, cwd, text[:60])
    with _jobs_lock:
        _jobs[sid] = {"status": "running", "started": time.time()}
    t = threading.Thread(target=_run_injection, args=(sid, cwd, text, False), daemon=True)
    t.start()
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


# ---------------------------------------------------------------- push

VAPID = json.load(open(Path(__file__).parent / "vapid.json"))
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
    """Send a web-push notification to all subscribed devices."""
    from pywebpush import webpush, WebPushException

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
APNS_TOKENS_PATH = Path(__file__).parent / "apns_tokens.json"
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


def send_apns(title, body, dir_="", sid="", badge=None):
    """Send a push to every registered device via APNs HTTP/2.

    If this Mac has no APNs signing key (every user except the app owner),
    deliver through the Ground Control relay instead — zero user setup,
    same as any mainstream app's notification server."""
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
    tokens = _apns_tokens()
    if body.token and body.token not in tokens:
        tokens.append(body.token)
        json.dump(tokens, open(APNS_TOKENS_PATH, "w"))
    return {"ok": True, "count": len(tokens)}


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
            "mute_remaining": remaining}


class SettingsBody(BaseModel):
    alert_delay: int = -1   # -1 = leave unchanged
    repeat_alert: int = -1  # seconds between re-alerts for unread; 0 = never


@app.post("/api/settings")
def set_settings(body: SettingsBody):
    s = _settings()
    if body.alert_delay >= 0:
        s["alert_delay"] = max(0, min(3600, body.alert_delay))
    if body.repeat_alert >= 0:
        s["repeat_alert"] = max(0, min(7200, body.repeat_alert))
    json.dump(s, open(SETTINGS_PATH, "w"))
    return {"ok": True, "alert_delay": s.get("alert_delay", 60), "repeat_alert": s.get("repeat_alert", 0)}


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


def _tsig(path):
    """Signature of the last real conversation turn (ignores title/metadata writes)."""
    last = None
    count = 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") in ("user", "assistant") and not d.get("isSidechain") and d.get("uuid"):
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


@app.post("/api/session-event")
def session_event(body: EventBody):
    """A session stopped or asked for input — arm a delayed alert."""
    _last_stop[body.session_id] = time.time()
    with _pending_lock:
        _pending[body.session_id] = {
            "fire_at": time.time() + alert_delay(),
            "sig": _tsig(body.transcript),
            "transcript": body.transcript,
            "title": body.title,
            "event": body.event,
            "body": body.message,
            "dir": body.dir,
        }
    return {"ok": True, "armed_in": alert_delay()}


_quiet_alerted = {}  # session_id -> mtime we already alerted for
_SERVER_START = time.time()


def _watchdog_check():
    """Catch sessions that died mid-turn (crash, credit cutoff, interrupt) —
    they never fire a Stop hook, so without this the user sits in limbo."""
    now = time.time()
    idx = _transcript_index()
    for sid, pid in live_sessions().items():
        path = idx.get(sid)
        if path is None:
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
        if _quiet_alerted.get(sid) == mtime:
            continue  # already alerted for this exact state
        with _pending_lock:
            if sid in _pending:
                continue
        _quiet_alerted[sid] = mtime
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


def _alert_worker():
    while True:
        time.sleep(5)
        try:
            _watchdog_check()
            _repeat_check()
        except Exception as e:  # noqa: BLE001
            print(f"[alert] watchdog error: {e}", flush=True)
        now = time.time()
        due = []
        with _pending_lock:
            for sid, p in list(_pending.items()):
                if now >= p["fire_at"]:
                    due.append((sid, p))
                    del _pending[sid]
        for sid, p in due:
            # if a new turn landed since the stop, you responded → skip
            now_sig = _tsig(p["transcript"])
            if now_sig != p["sig"]:
                print(f"[alert] suppressed {sid[:8]} (activity {p['sig']} -> {now_sig})", flush=True)
                continue
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
            print(f"[alert] FIRED {sid[:8]} -> web:{n_web} apns:{n_apns} badge:{badge}: {title}", flush=True)


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
def serve_msg_image(project_dir: str, session_id: str, uuid: str, idx: int):
    """Serve an image embedded (pasted) inside a transcript message."""
    import base64

    from fastapi import Response

    path = PROJECTS_DIR / project_dir / f"{session_id}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    for e in _read_lines(path):
        if e.get("uuid") == uuid:
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and 0 <= idx < len(content):
                b = content[idx]
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
