"""
Ground Control — EZ (raw-PTY) session engine wrapper.

The TERMINAL is the brain: each session is `claude` running inside an EZ PTY
daemon (gc_ez_engine.py — Phil's tmux-alternative that streams raw bytes, so a
real terminal emulator renders it natively). The app is just a window in:
  - browser/native xterm renders the raw stream  (what it's doing, the truth)
  - typing → raw bytes back into the PTY          (reliable delivery)
  - "working" = Claude's own status line in the stream (never inferred)

This wrapper lets the FastAPI server start/kill EZ sessions and bridge a
WebSocket <-> the EZ unix socket.
"""
from __future__ import annotations

import os
import re as _re
import socket
import struct
import subprocess
import sys
import time
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
import gc_ez_engine as ez  # noqa: E402


def socket_path(name: str) -> str:
    return ez.socket_path(name)


def is_alive(name: str) -> bool:
    return ez.is_session_alive(name)


def list_sessions() -> list[str]:
    try:
        return [f[:-5] for f in os.listdir(ez.SOCKET_DIR)
                if f.endswith(".sock") and ez.is_session_alive(f[:-5])]
    except OSError:
        return []


def start(name: str, cwd: str, command: list[str]) -> None:
    """Spawn an EZ daemon running `command` (detached). No-op if already alive."""
    if is_alive(name):
        return
    os.makedirs(ez.SOCKET_DIR, exist_ok=True)
    # Run daemon_main in a throwaway python that itself double-forks into the
    # background daemon and exits — keeps the server process clean.
    code = (
        "import sys;sys.path.insert(0,%r);import os;"
        "os.chdir(%r);import gc_ez_engine as ez;"
        "ez.daemon_main(%r, %r)" % (_DIR, cwd, name, command)
    )
    subprocess.Popen([sys.executable, "-c", code], cwd=cwd,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    # wait briefly for the socket to appear
    sp = socket_path(name)
    for _ in range(40):
        if os.path.exists(sp):
            break
        time.sleep(0.05)


def kill(name: str) -> None:
    try:
        ez.kill_session(name)
    except Exception:  # noqa: BLE001
        pass


def connect_client(name: str, cols: int = 80, rows: int = 40,
                   timeout: float = 5.0, live_only: bool = False,
                   snapshot: bool = False) -> Optional[socket.socket]:
    """Open a client connection to the EZ daemon.

    Normal: the daemon first replays its whole ring buffer (history), then streams live
    output. That's what a terminal window wants.

    live_only=True (flag 0x04, ACK 0x06): SKIP the history replay, stream live output
    only. The working-state monitor uses this — with no history on the wire, every byte
    it receives is live BY CONSTRUCTION, so replayed history can never be mistaken for
    the session doing work.

    snapshot=True (flag 0x02): send the last 64KB of the buffer then CLOSE. One-shot
    screen peek — EOF means "you have the complete current tail", so readers terminate
    deterministically instead of guessing with a time budget, and the daemon never
    flushes multi-MB history for a peek.

    Daemons started before these flags existed treat both as a normal client (full
    replay, no close) — callers keep their budget/drain fallbacks for that case.
    """
    sp = socket_path(name)
    if not os.path.exists(sp):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sp)
        flag = b"\x04" if live_only else (b"\x02" if snapshot else b"\x00")
        s.sendall(struct.pack("!HH", cols, rows) + flag)
        return s
    except OSError:
        try:
            s.close()
        except OSError:
            pass
        return None


def send_input(name: str, text: str) -> bool:
    """One-shot: type `text` into the EZ PTY WITHOUT disturbing its size (size 0,0
    tells the daemon to skip the resize). Used when the message view sends to a
    session whose brain is a live terminal."""
    import select as _select
    sp = socket_path(name)
    if not os.path.exists(sp):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(sp)
        s.sendall(struct.pack("!HH", 0, 0) + b"\x00")  # size 0 = don't resize
        s.setblocking(False)
        # drain the initial buffer dump so we don't wedge the daemon's sendall
        t0 = time.time()
        while time.time() - t0 < 1.0:
            r, _, _ = _select.select([s], [], [], 0.15)
            if not r:
                break
            try:
                if not s.recv(65536):
                    break
            except (BlockingIOError, OSError):
                break
        s.setblocking(True)
        s.settimeout(3.0)
        s.sendall(text.encode())
        time.sleep(0.05)
        s.close()
        return True
    except OSError:
        return False


def resize(name: str, cols: int, rows: int) -> None:
    """Send a resize-only header to the daemon (flag 0x01)."""
    sp = socket_path(name)
    if not os.path.exists(sp):
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(sp)
        s.sendall(struct.pack("!HH", cols, rows) + b"\x01")
        s.close()
    except OSError:
        pass


# --- "working" detection: read Claude's own status line from the raw stream ---

# An ACTIVE spinner status line: a spinner glyph followed (within ~60 chars) by a
# gerund '…' (Kneading…, Thinking…) or tool context. Some sessions/versions/widths
# show 'esc to interrupt', others only show this spinner — we accept EITHER.
_ACTIVE_RE = _re.compile(r'[✻✽✶✳✢✷✵✴✸✹✺✼◐◑◒◓][^\r\n]{0,60}?(…|still running|background agent|shell)')

# The set of glyphs Claude cycles through in its spinner. When working, this glyph
# changes every ~0.1-0.3s; when idle a leftover frame keeps ONE glyph forever.
_SPIN_GLYPHS = '✻✽✶✳✢✷✵✴✸✹✺✼◐◑◒◓'


def _spinner_line(tail):
    """Return the last line that carries a spinner glyph (stripped), else None.
    This is Claude's own status line ('✻ Ruminating…', '✽ Determining… (12s · ↓3k)').
    Isolating just this line lets us tell a LIVE spinner (glyph/timer changes between
    reads) from a FROZEN leftover frame (identical) — without whole-tail cursor noise."""
    if not tail:
        return None
    for ln in reversed(tail.splitlines()):
        if any(g in ln for g in _SPIN_GLYPHS):
            return ln.strip()
    return None


def _tail_is_working(tail) -> bool:
    """True iff the terminal is actively generating / running a tool. Two accepted
    signals (they vary by CC version / terminal width): the 'esc to interrupt' footer,
    OR an active spinner status line. A finished 'Worked for 25s' summary has neither."""
    if not tail:
        return False
    # Footer only in the live bottom region — a stale "esc to interrupt" up in the
    # raw byte tail (old redraw frame) must not read as working (the Lowes bug).
    if "esctointerrupt" in _re.sub(r"\s+", "", tail[-220:]).lower():
        return True
    return _ACTIVE_RE.search(tail) is not None


def snapshot(name: str, cols: int = 80, rows: int = 40) -> bytes:
    """Grab the current buffered output (raw bytes) without holding the stream.

    Connects PASSIVELY (size 0,0) — a snapshot is a read-only peek and must NEVER
    resize the PTY or steal the active-client crown (that yanked new sessions to
    the snapshot size and flapped the mobile terminal). cols/rows are kept in the
    signature for compatibility but intentionally ignored."""
    s = connect_client(name, 0, 0, timeout=3.0)
    if not s:
        return b""
    buf = b""
    s.settimeout(0.6)
    t0 = time.time()
    try:
        while time.time() - t0 < 1.2:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
            if len(buf) > 200_000:      # keep only the recent screen — the ring
                buf = buf[-64_000:]     # buffer can be up to 2MB; we want the tail
    finally:
        try:
            s.close()
        except OSError:
            pass
    return buf


import re as _re  # noqa: E402

_ANSI_RE = _re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][AB0]")
_work_cache: dict = {}   # name -> (ts, working:bool|None, label:str|None)
# TTL governs how often the warmer actually SNAPSHOTS each session (a snapshot replays
# the daemon's ring buffer — up to 2MB for a heavy session — and briefly blocks the PTY
# stream the phone terminal is reading). 0.8s meant a hitch ~once/sec on the session
# you're watching. 1.5s halves those hitches; the dot/banner still update within ~1.5s,
# which is plenty for a spinner-vs-gray indicator.
_WORK_TTL = 1.5


def _quick_tail(name: str, n: int = 800) -> Optional[str]:
    """Fast read of the terminal's current tail (plain text). None if unreachable.

    MUST connect PASSIVELY (size 0,0) — a nonzero size on a real-client connect
    makes the daemon mark this peek as the ACTIVE client and resize the PTY to it
    (tmux-style most-recent-active sizing). Since this runs every 1.5s for every
    session, a nonzero size yanked the PTY to that size and back on every poll —
    the mobile terminal's size-flap / flicker AND the ~90-col wide-wrap. 0,0 tells
    the daemon: don't resize, don't steal active — just give me the buffer.

    snapshot=True: a current daemon sends the last 64KB and CLOSES — the loop below
    exits at EOF with the exact current tail. An old daemon ignores the flag and
    replays everything, so the time-budget cap stays as its fallback."""
    s = connect_client(name, 0, 0, timeout=1.5, snapshot=True)
    if not s:
        return None
    buf = b""
    s.settimeout(0.12)
    t0 = time.time()
    try:
        while time.time() - t0 < 0.4:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
            if len(buf) > 300_000:
                buf = buf[-120_000:]
    finally:
        try:
            s.close()
        except OSError:
            pass
    return _plain(buf[-4000:])[-n:]


def _plain(b: bytes) -> str:
    return _ANSI_RE.sub(b"", b).decode("utf-8", "replace")


# --- REAL-SCREEN render (the iron working-state source) ---------------------
# The raw PTY buffer CONCATENATES redraw frames: Claude's TUI repaints with
# absolute cursor moves, not a clean wipe, so a finished turn's "esc to interrupt"
# footer stays in the byte tail RIGHT NEXT TO the fresh idle "❯" prompt. Every
# substring scan of the raw bytes (even a last-220-char window) eventually misfires
# on that leftover and reports an idle session as "working" — the phantom-spinner
# bug that keeps coming back. The ONLY reliable read of "what's actually on screen"
# is to emulate the stream exactly like xterm.js / SwiftTerm do on the client:
# feed the bytes to a real VT emulator (pyte) and read the final visible grid. Every
# cursor-addressed repaint collapses to the last frame; no stale footer survives.
try:
    import pyte as _pyte  # noqa: E402
except Exception:          # pragma: no cover - pyte should be installed
    _pyte = None


def _quick_raw(name: str, budget: float = 0.4, cap: int = 160_000) -> Optional[bytes]:
    """Fast RAW-byte read of the terminal buffer (pyte needs bytes, not the
    ANSI-stripped text _quick_tail returns). Passive connect (0,0) — never resizes
    or steals the active-client crown. Returns the recent tail; the last full repaint
    lives within it and, being absolutely-positioned, overwrites any partial leading
    frame when emulated. snapshot=True → current daemons send 64KB + close (EOF ends
    the loop, exact tail); old daemons replay everything and the budget caps it."""
    s = connect_client(name, 0, 0, timeout=1.5, snapshot=True)
    if not s:
        return None
    buf = b""
    s.settimeout(0.12)
    t0 = time.time()
    try:
        while time.time() - t0 < budget:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
            if len(buf) > cap * 2:
                buf = buf[-cap:]
    finally:
        try:
            s.close()
        except OSError:
            pass
    return buf[-cap:] if buf else None


# Begin-Synchronized-Update. Claude's TUI wraps each repaint in BSU … ESU and redraws
# the whole prompt region inside that block — so the last BSU block IS the current screen.
# Only used as a fallback when the true PTY geometry can't be determined.
_BSU = b"\x1b[?2026h"

_size_cache: dict = {}      # name -> (ts, (cols, rows))
_SIZE_TTL = 15.0


def _pty_size(name: str):
    """The session's TRUE PTY geometry (cols, rows), or None.

    pyte MUST render at this exact size. A TUI positions text with ABSOLUTE cursor moves
    that reference the real terminal size, so rendering into a differently-shaped grid
    desyncs the replay and progressively corrupts it. Measured on the /model wizard: at
    220x60 the menu came out as "Opusl4.8 withE1Micontext" with only 3 of 5 options; at the
    real 80x24 it renders perfectly. Widening the grid (the old "render bigger so nothing
    clips" idea) is exactly wrong — it guarantees the mismatch.

    Read from the OS, not the wire: find this session's daemon, then the child (claude) on
    its PTY slave, and ioctl the tty. Works for daemons of ANY vintage — no protocol change,
    so sessions already running get the fix too.
    """
    hit = _size_cache.get(name)
    if hit and time.time() - hit[0] < _SIZE_TTL:
        return hit[1]
    size = None
    try:
        import fcntl as _fcntl
        import termios as _termios

        out = subprocess.run(["ps", "-Ao", "pid,ppid,tty,command"],
                             capture_output=True, text=True, timeout=3).stdout
        dpid = None
        for line in out.splitlines():
            if "gc_ez_engine" in line and name in line:
                dpid = line.split()[0]
                break
        if dpid:
            for line in out.splitlines():
                p = line.split(None, 3)
                if len(p) >= 3 and p[1] == dpid and p[2].startswith("ttys"):
                    fd = os.open("/dev/" + p[2], os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        rows, cols, _, _ = struct.unpack(
                            "HHHH", _fcntl.ioctl(fd, _termios.TIOCGWINSZ, b"\0" * 8))
                    finally:
                        os.close(fd)
                    if cols > 0 and rows > 0:
                        size = (cols, rows)
                    break
    except Exception:  # noqa: BLE001
        size = None
    _size_cache[name] = (time.time(), size)
    return size


def _render_frame(raw: bytes, cols: int, rows: int) -> Optional[list]:
    scr = _pyte.Screen(cols, rows)
    stream = _pyte.ByteStream(scr)
    try:
        stream.feed(raw)
    except Exception:  # noqa: BLE001
        return None
    return [ln.rstrip() for ln in scr.display]


def _render_lines(name: str, cols: int = 0, rows: int = 0) -> Optional[list]:
    """Render the terminal's CURRENT screen (rstripped lines) via pyte.

    RENDER AT THE TRUE PTY GEOMETRY (see _pty_size). That single fact is what makes the
    replay faithful — it's how a real client (xterm.js/SwiftTerm) gets a correct screen from
    the same bytes. Feed the recent tail; successive full repaints converge onto the current
    screen, exactly as they do in the user's terminal window.

    Fallback when the size can't be read: render from the last synchronized-update frame
    (Claude wraps each repaint in BSU…ESU, so that block is a coherent screen) on a wide
    grid. Less faithful — cells the TUI didn't rewrite come out blank — but far better than
    replaying history into a mismatched grid, which corrupts the text outright.
    """
    if _pyte is None:
        return None
    raw = _quick_raw(name)
    if not raw:
        return None
    size = _pty_size(name) if not (cols and rows) else (cols, rows)
    if size:
        return _render_frame(raw[-64_000:], size[0], size[1])
    starts, i = [], raw.rfind(_BSU)
    while i != -1 and len(starts) < 6:
        starts.append(i)
        i = raw.rfind(_BSU, 0, i)
    for s in starts:
        lines = _render_frame(raw[s:], 220, 60)
        if lines and sum(1 for ln in lines if ln.strip()) >= 3:
            return lines
    return _render_frame(raw[-48_000:], 220, 60)


_SPIN_GLYPHS_SET = "✻✽✶✳✢✷✵✴✸✹✺✼◐◑◒◓"


def _footer_says_working(lines: list) -> bool:
    """True iff the RENDERED screen shows Claude actively generating / running a tool.
    Reads the real status line, not just the footer text — because:
      • On a NARROW terminal (phone) Claude truncates the bottom bar, so 'esc to
        interrupt' becomes '· e…' and a plain substring match misses it.
      • The reliable, width-independent signal is the STATUS LINE: a spinner glyph
        followed by a GERUND ellipsis — '✳ Fluttering…', '✻ Kneading… (12s · ↓3k)'.
        A FINISHED turn shows past tense with NO ellipsis — '✻ Worked for 37s',
        '✻ Cogitated for 6m 23s' — so the '…' cleanly separates working from done.
    Working iff either signal is present in the bottom region of the rendered screen."""
    nonblank = [ln for ln in lines if ln.strip()]
    for ln in nonblank[-14:]:                   # status line sits just above the input box
        # THE reliable signal is Claude's ACTIVE STATUS LINE: a gerund ellipsis '…' WITH a
        # live elapsed timer — 'Marinating… (10s · ↓341 tokens)', 'Fluttering… (1m 37s …)'.
        # It exists ONLY while generating / running a tool and vanishes the instant the turn
        # ends. We deliberately DON'T trust 'esc to interrupt' in the bottom bar: Claude
        # leaves that string there STALE after a turn finishes (the FA: Marketing phantom —
        # idle '❯' prompt, no status line, but a leftover 'esc to interrupt'). The spinner
        # glyph is unreliable too (renders as '·' at some widths), so key off '…' + '(<n>'.
        if "…" in ln and _re.search(r"\(\d", ln):
            return True
    return False


def _status_line(lines: list):
    """The active status line — a gerund '…' with a live elapsed timer '(<n>…' — or None.
    e.g. '✳ Marinating… (10s · ↓341 tokens)'. Only present while generating/running a tool."""
    if not lines:
        return None
    for ln in [l for l in lines if l.strip()][-14:]:
        if "…" in ln and _re.search(r"\(\d", ln):
            return ln.strip()
    return None


# Per-session freeze detector: a status line that stays byte-identical across renders is a
# FROZEN leftover (hung/crashed turn), NOT live work. sid -> (last_status_line, since_ts).
_status_state: dict = {}
_STATUS_FREEZE = 3.0   # seconds a status line may sit UNCHANGED before it's ruled frozen


def _is_status_live(name: str, status: Optional[str], now: float) -> bool:
    """True iff `status` is present AND changing (advancing timer/tokens). A frozen line
    (same text for > _STATUS_FREEZE s) or an absent one → not working. Uses successive
    renders (the warmer calls this ~every 1.5s) rather than an inline sleep."""
    if status is None:
        return False        # idle now — KEEP state so a re-appearing frozen line stays frozen
                            # (FA flapped idle↔'10s'; popping would reset the freeze clock)
    st = _status_state.get(name)
    if st is None or st[0] != status:
        _status_state[name] = (status, now)      # genuinely NEW/changed status → live
        return True
    return (now - st[1]) <= _STATUS_FREEZE        # same line unchanged: live only if still fresh


def _label_from_lines(lines: list) -> Optional[str]:
    """Claude's own status word from the rendered screen — 'Fluttering…', 'Kneading…'
    — stripped of the glyph and the '(12s · ↓3k tokens)' timer (unreliable/frozen; the
    caller shows a clean one). None if no active status line. Read from the SAME rendered
    lines is_working already has, so the label costs ZERO extra snapshots (a per-poll
    _quick_tail here on the viewed session's busy socket was what made phone typing lag)."""
    sl = _status_line(lines)                    # e.g. '· Marinating… (10s · ↓341 tokens)'
    if not sl:
        return None
    s = sl
    for g in _SPIN_GLYPHS_SET:
        s = s.replace(g, "")
    s = s.lstrip(" ·")
    i = s.find("(")                             # drop the '(10s · ↓341 tokens)' timer
    if i > 0:
        s = s[:i]
    return s.strip() or None


# ============================================================================
# WORKING-STATE = OUTPUT ACTIVITY  (the terminal is the boss)
# See .claude/docs/ALERTS_AND_WORKING_STATE.md. A session is "working" iff its PTY is
# actively EMITTING output: while Claude works, its spinner animates and text streams
# continuously; the instant it's done, output stops. We detect that with ONE passive
# streaming connection per session — no repeated buffer replays, no screen parsing.
# So it mirrors the terminal EXACTLY, never flickers, and a hung session (no output)
# reads idle for free. Validated: idle terminals are byte-silent.
# ============================================================================
import threading as _threading

_activity_ts: dict = {}         # name -> wall-clock time of the last live output byte
_activity_threads: dict = {}    # name -> live monitor thread
_armed: set = set()             # names whose monitor is PAST the history replay (see below)
_activity_lock = _threading.Lock()
ACTIVE_WINDOW = 0.6             # working = output seen within this many seconds (the one knob)

# Arming (only needed for daemons started before the live-only flag existed — they still
# replay their history on connect). An un-armed monitor stamps nothing and is_busy() is
# False, so a replay can never be mistaken for work no matter how long it takes to drain.
_REPLAY_QUIET = 2.0             # silence this long ⇒ the history flush is over
_ARM_CAP = 30.0                 # never spend longer than this arming


def _activity_monitor(name: str):
    """Hold a PASSIVE connection and stamp _activity_ts on every LIVE output byte.

    THE HISTORY-REPLAY TRAP — this is what produced the phantom "working" spinner, twice.
    On a normal connect the daemon flushes its ENTIRE ring buffer first (measured: 600KB /
    ~6.5s on a long session like `shopping lowes`). Those bytes are HISTORY, not live
    output. The old code tried to skip them by waiting for a 0.4s quiet gap — but the flush
    stalls longer than that, so the drain ended early and the REST OF THE HISTORY got
    stamped as live activity → an idle session read BUSY. And because ensure_monitors()
    respawns a dropped monitor, it reconnected, replayed, and spun again — forever.

    Two defenses, so this class of bug cannot come back:
      1. LIVE-ONLY connect (flag 0x04). A current daemon skips the replay entirely and ACKs
         with 0x06 → there is NO history on the wire → every byte is live BY CONSTRUCTION.
         No timing guess anywhere.
      2. Daemons already running (started before the flag) still replay. For them we ARM by
         draining to a genuinely quiet gap. Replay and live output can never interleave —
         the daemon is blocked inside its sendall while flushing — so that silence is a
         clean cut.

    INVARIANT: an UN-ARMED monitor stamps NOTHING, and is_busy() reports False for a session
    whose monitor isn't armed. So history — however long it takes to drain, and however many
    times we reconnect — can never be counted as work.
    """
    s = connect_client(name, 0, 0, timeout=3.0, live_only=True)  # passive: never steals active/resizes
    if s is None:
        with _activity_lock:
            _activity_threads.pop(name, None)
        return
    try:
        armed = False
        # Did the daemon honour live-only? It ACKs 0x06 before anything else.
        s.settimeout(1.0)
        try:
            if s.recv(1) == b"\x06":
                armed = True          # no history on this wire → every byte is live
        except socket.timeout:
            pass                      # silent so far; treat as an old daemon and drain
        except OSError:
            return
        if not armed:
            # Old daemon: it is flushing history at us. Drain to a quiet gap, stamping NOTHING.
            s.settimeout(_REPLAY_QUIET)
            t0 = time.time()
            while time.time() - t0 < _ARM_CAP:
                try:
                    if not s.recv(65536):
                        return        # daemon closed → session gone
                except socket.timeout:
                    break             # quiet → the history flush is over
                except OSError:
                    return
        with _activity_lock:
            _armed.add(name)
        # Live stream: every byte now means the terminal is producing output RIGHT NOW.
        s.settimeout(120.0)
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue          # long quiet is fine; keep the connection open
            except OSError:
                break
            if not d:
                break             # daemon closed → session gone
            _activity_ts[name] = time.time()
    finally:
        try:
            s.close()
        except OSError:
            pass
        with _activity_lock:
            _activity_threads.pop(name, None)
            _armed.discard(name)      # a respawned monitor must RE-ARM before it can report busy
            _activity_ts.pop(name, None)


def ensure_monitors() -> None:
    """Ensure every live session has an activity monitor; drop state for dead ones.
    Cheap — safe to call on the warmer's cadence."""
    live = set(list_sessions())
    with _activity_lock:
        for name in live:
            t = _activity_threads.get(name)
            if t is None or not t.is_alive():
                t = _threading.Thread(target=_activity_monitor, args=(name,), daemon=True)
                _activity_threads[name] = t
                t.start()
        for name in list(_activity_ts):
            if name not in live:
                _activity_ts.pop(name, None)


def is_busy(name: str) -> bool:
    """True iff the terminal emitted LIVE output within ACTIVE_WINDOW — i.e. it is running
    right now. Mirrors the terminal with no hold and no screen parsing. (Background jobs
    are OR'd in at the server layer.)

    HARD GUARD: a session whose monitor has not finished skipping the connect-time history
    replay is NOT busy. Without this, replayed history reads as work — the phantom spinner.
    Not-armed ⇒ we cannot know ⇒ False. A false "idle" for a second is always better than a
    false "working" (Phil's rule), and it self-corrects the moment the monitor arms."""
    with _activity_lock:
        if name not in _armed:
            return False
    ts = _activity_ts.get(name)
    return ts is not None and (time.time() - ts) < ACTIVE_WINDOW


def is_working(name: str, allow_snapshot: bool = True, force: bool = False) -> bool:
    """Back-compat alias — working-state is now pure output activity (`is_busy`). The
    allow_snapshot/force flags are no-ops (is_busy is always cheap and current)."""
    return is_busy(name)


# Banner label ("Fluttering…") — best-effort, throttled screen render. Only the
# actively-viewed session's banner needs it; the cheap side dots use is_busy only.
_label_cache: dict = {}   # name -> (ts, label)
_LABEL_TTL = 1.5


def work_label(name: str, render: bool = True) -> Optional[str]:
    """Claude's current status word for the banner. None if idle.

    render=True (the warmer): actually render when the cache is stale — this is the ONLY
    place a render should happen, on the background thread.
    render=False (request path, via work_status): pure CACHE read — never do a 0.4s socket
    render inline. Rendering on the request made /api/work ~545ms/poll and fought the live
    terminal for the daemon socket. A slightly stale label is invisible; a stalled poll is not."""
    if not is_busy(name):
        return None
    hit = _label_cache.get(name)
    now = time.time()
    if hit and now - hit[0] < _LABEL_TTL:
        return hit[1]
    if not render:
        return hit[1] if hit else None      # stale-but-cheap; warmer refreshes it
    lines = _render_lines(name)
    label = _label_from_lines(lines) if lines else None
    _label_cache[name] = (now, label)
    return label


# --- "waiting for input" detection: Claude is BLOCKED on an interactive prompt ---
# (AskUserQuestion multi-select, tool-permission prompt, folder-trust prompt). This
# is the app's #1 signal — "you're stuck waiting on me" — to alert + surface in chat.
# Signatures are footer/prompt text that only appears while a prompt is on screen.
# NOTE: "esc to interrupt" is the WORKING footer (generating), NOT a prompt — excluded.
# Markers are SPACELESS + lowercased: a TUI positions text with cursor moves, not
# spaces, so once ANSI is stripped the word gaps vanish ("Esc to cancel" -> "esctocancel").
# Order matters — more specific labels first.
_WAITING_MARKERS = (
    ("doyouwanttoproceed", "permission"),
    ("trustthisfolder", "trust"),
    ("doyoutrust", "trust"),
    ("entertoselect", "question"),        # AskUserQuestion / select list
    ("entertoconfirm", "prompt"),         # trust / yes-no confirm
    ("esctocancel", "question"),          # universal select/question/prompt footer
)


def waiting_for_input(name: str):
    """Return a short label ('question' | 'permission' | 'trust' | 'prompt') if the
    terminal is BLOCKED on an interactive prompt, else None. Read live from the
    terminal — a waiting prompt is static, so one passive tail read catches it."""
    if not is_alive(name):
        return None
    # SMALL window: an ACTIVE prompt's footer is the last thing on screen. A larger
    # window would keep matching the prompt text sitting in scrollback AFTER it was
    # answered (false positive). ~700 chars ≈ the bottom of the current screen.
    if is_busy(name):                         # actively generating/running → not a prompt
        return None
    tail = _quick_tail(name, n=700)
    if not tail:
        return None
    low = _re.sub(r"\s+", "", tail).lower()   # collapse TUI cursor-positioned gaps
    for marker, label in _WAITING_MARKERS:
        if marker in low:
            return label
    return None


# A numbered menu row on the rendered screen: "1. Yes", "❯ 2. No, tell Claude…", "3) Skip".
_OPTION_RE = _re.compile(r'^\s*[❯>*·]?\s*(\d)[.)]\s+(\S.*?)\s*$')
_BOX_CHARS = " │┃|╭╮╰╯─━┌┐└┘├┤┬┴┼║╔╗╚╝═\t"


def _strip_box(line: str) -> str:
    """Drop the TUI box-drawing border from both ends of a rendered row.
    Claude renders prompts inside a box, so a menu row arrives as '│ ❯ 1. Yes … │'."""
    return line.strip(_BOX_CHARS)


def _split_label(rest: str):
    """Split a menu row's text into (label, description). Claude column-aligns the two
    with a run of spaces — '1. Fable        Fable 5 · Most capable…' — so a 2+ space gap
    is the separator. Without this the app showed one long run-on line per option."""
    parts = _re.split(r"\s{2,}", rest, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def terminal_question(name: str):
    """Read the prompt Claude is BLOCKED on straight off the rendered screen, as
    {question, options[]} — so the app can offer TAPPABLE answers for ANY interactive
    prompt, not just the structured AskUserQuestion tool.

    Why this exists: the app used to fall back to a dead-end "Answer in Terminal" button
    whenever the prompt wasn't an AskUserQuestion (permission prompts, trust prompts, and
    Claude's own numbered menus). Being punted into the terminal is the thing Phil hates —
    the whole point of the app is that you're never stuck.

    Safe to render here (unlike working-state): a blocked prompt is STATIC — it isn't
    repainting — so there is no mid-repaint frame to catch. Answering just types the option
    NUMBER, which is exactly what these menus accept.
    """
    if not waiting_for_input(name):
        return None
    lines = _render_lines(name)
    if not lines:
        return None
    # Claude draws these menus INSIDE a box, so rows arrive as "│ ❯ 1. Yes … │".
    # Strip the border glyphs before matching — otherwise nothing matches and we silently
    # fall back to the dead-end "Answer in Terminal" button.
    #
    # Scan the WHOLE rendered screen, not a bottom slice. We used to look only at the last
    # 30 rows, but pyte renders a 60-row grid and the TUI draws from the TOP — the /model
    # wizard's options land around rows 20-27, so the tail window saw only blank rows and
    # every /model, /effort, etc. prompt fell through to "answer in the terminal".
    rows = [_strip_box(l) for l in lines]
    # Collect contiguous 1,2,3… runs. CRITICAL: the LAST run wins. Claude's own message
    # text often contains a numbered list ("my plan: 1. Foo  2. Bar") that sits on screen
    # ABOVE the real menu — locking onto the first run would show those lines as the
    # options while the typed digit answers the REAL menu below: a label/action mismatch,
    # the worst possible failure (silently answers the wrong thing). The prompt's menu is
    # always the bottom-most run, so every fresh "1." starts a new candidate run.
    opts, first_idx, last_row = [], None, None
    for i, line in enumerate(rows):
        m = _OPTION_RE.match(line)
        if m:
            n, rest = int(m.group(1)), m.group(2).strip()
            if n == 1:                    # a new "1." = a new candidate menu — reset
                opts, first_idx = [], i
            elif n != len(opts) + 1:      # non-contiguous → not part of the current run
                continue
            label, desc = _split_label(rest)
            opts.append({"label": label, "description": desc})
            last_row = i
            continue
        # A wrapped continuation of the option directly above ("… Best for everyday," /
        # "complex tasks") — fold it into that option's description. A blank row always
        # separates the menu from the footer below, so this can't swallow the footer.
        if opts and last_row == i - 1 and line.strip():
            opts[-1]["description"] = (opts[-1]["description"] + " " + line.strip()).strip()
            last_row = i
    if len(opts) < 2:                 # not a menu we can answer by number
        return None
    # The question/title = the TOP line of the contiguous text block above the options
    # (skip the blank gap first). Taking the nearest line gave the wrapped tail of the
    # blurb ("other/previous model names, specify with --model.") instead of "Select model".
    # A separator/border row strips to "" so the walk stops there naturally.
    question = ""
    j = (first_idx or 0) - 1
    while j >= 0 and not rows[j].strip():
        j -= 1
    block = []
    while j >= 0 and rows[j].strip():
        block.append(rows[j].strip())
        j -= 1
    if block:
        question = block[-1]
    return {"header": "", "question": question, "multiSelect": False, "options": opts}


# The whimsical status word + any context Claude shows on its spinner line.
_SPIN = "✻✽✶✳✢✷✵✴✸✹✺✼"
_STATUS_RE = _re.compile(
    r'[' + _SPIN + r']\s*'                    # spinner glyph
    r'([A-Za-z][a-zA-Z]+…?)'                  # the status word (Flowing…, Cogitated, Thinking…)
    r'([^\n]*?(?:shell|agent|tool|running|thought)[^\n]{0,40})?'  # optional meaningful context
)
# Noise to strip from the extracted label (the terminal's own timer/tokens — unreliable;
# we show a clean server-computed elapsed instead).
_TIMER_NOISE = _re.compile(r'\(?\s*(?:for\s+)?\d+m?\s*\d*s\b|·?\s*↓?\s*[\d.]+k?\s*tokens?|thought for \d+s|[()]')


def _extract_label(tail) -> Optional[str]:
    """Pull Claude's status word + context from a tail ('Cogitated · 1 shell still
    running'), timer/tokens stripped (caller adds a clean one). No working-check —
    the caller gates on is_working. None if nothing clean is found."""
    if not tail:
        return None
    best = None
    for line in tail.splitlines():
        m = _STATUS_RE.search(line)
        if not m:
            continue
        word = m.group(1)
        ctx = _TIMER_NOISE.sub("", m.group(2) or "").strip(" ·\t")
        label = (word + (" · " + ctx if ctx else "")).strip()
        if best is None or len(label) > len(best):
            best = label
    return best


def work_status(name: str):
    """(working, label). Working = live output activity (`is_busy`); label = throttled
    render. ONE source used by chat banner, terminal banner, and side dot."""
    if not is_busy(name):
        return (False, None)
    return (True, work_label(name, render=False))   # cache-only on the request path


def refresh_working(names: list) -> None:
    """Back-compat entry for the server warmer: just keep the activity monitors alive.
    There is no per-call snapshot anymore — the monitors stream output continuously and
    stamp _activity_ts, so working-state is event-driven, not polled."""
    ensure_monitors()
