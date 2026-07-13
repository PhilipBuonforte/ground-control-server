#!/usr/bin/env python3
"""
eztmux — Minimal terminal session persistence with raw byte passthrough.
Unlike tmux, this does NOT re-render the screen. It just holds a PTY open,
buffers raw output bytes, and streams them to all connected clients.
SwiftTerm (or any terminal) handles rendering natively — scrolling works.

Usage:
    eztmux start <name> [command...]   Create a new session
    eztmux attach <name>               Attach to existing session
    eztmux ls                          List active sessions
    eztmux kill <name>                 Kill a session
    eztmux <name> [command...]         Create-or-attach (shorthand)

Multiple clients can attach simultaneously — all see the same output in
real time. Walk from laptop to phone and back without detaching.
"""
import os, sys, pty, select, socket, signal, struct, tty, termios, fcntl, time, re

SOCKET_DIR = os.path.expanduser("~/.eztmux")
BUFFER_SIZE = 2 * 1024 * 1024  # 2MB ring buffer

def socket_path(name):
    return os.path.join(SOCKET_DIR, f"{name}.sock")

def pid_path(name):
    return os.path.join(SOCKET_DIR, f"{name}.pid")

def is_session_alive(name):
    """Check if a session is alive by looking for its PID."""
    ppath = pid_path(name)
    if not os.path.exists(ppath):
        return False
    try:
        with open(ppath) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = just check if alive
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        return False


# ============================================================
# DAEMON MODE — runs in background, manages PTY + clients
# ============================================================

def daemon_main(name, command):
    """Fork to background and manage a PTY session."""
    os.makedirs(SOCKET_DIR, exist_ok=True)
    spath = socket_path(name)

    # Clean up stale socket
    if os.path.exists(spath):
        if is_session_alive(name):
            print(f"eztmux: session '{name}' already exists", file=sys.stderr)
            sys.exit(1)
        else:
            os.unlink(spath)
            try: os.unlink(pid_path(name))
            except: pass

    # Fork to background
    if os.fork() > 0:
        time.sleep(0.5)
        return

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    # Redirect stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    # Create PTY
    master_fd, slave_fd = pty.openpty()
    set_pty_size(master_fd, 80, 24)

    # Fork child process
    child_pid = os.fork()
    if child_pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        env = os.environ.copy()
        # Advertise a synchronized-output-capable terminal so Claude Code wraps its
        # redraws in DEC mode 2026 (ESC[?2026h..ESC[?2026l). VERIFIED empirically:
        # Claude enables sync by TERM-NAME match — `xterm-ghostty` triggers it (25
        # frames in a test render), while a generic name WITH the terminfo Sync cap
        # does NOT, and Claude never sends a DECRQM probe. _SyncCoalescer then buffers
        # each frame and flushes it atomically, so even a v5 xterm.js / older
        # SwiftTerm client paints the whole redraw at once = no flicker. We ship our
        # own `xterm-ghostty` terminfo (use=xterm-256color, so identical caps + Sync)
        # and point TERMINFO at it so the name always resolves without Ghostty
        # installed. Falls back to plain xterm-256color if the terminfo is missing.
        _ti = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ezterminfo')
        if os.path.isdir(_ti):
            env['TERMINFO'] = _ti
            env['TERM'] = 'xterm-ghostty'
        else:
            env['TERM'] = 'xterm-256color'
        env['EZTMUX_SESSION'] = name
        os.execvpe(command[0], command, env)

    os.close(slave_fd)

    # Write PID file
    with open(pid_path(name), 'w') as f:
        f.write(str(os.getpid()))

    # Create server socket
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(spath)
    server.listen(5)
    server.setblocking(False)

    output_buffer = bytearray()
    clients = []  # list of client sockets
    client_sizes = {}  # fd -> (cols, rows)
    client_input_hold = {}  # fd.fileno() -> trailing partial GCSZ resize APC held across reads
    active_fd = [None]  # who last typed (list for inner scope mutation)
    pending = []  # new connections waiting for size header (list so inner scope can mutate)

    # Synchronized-output (DEC 2026) frame coalescer — see _SyncCoalescer. Answers
    # Claude's mode-2026 probe and buffers each h..l redraw into one atomic flush
    # so the mobile terminal stops flickering. reply_writer feeds the DECRQM answer
    # back into the PTY (Claude's stdin).
    sync = _SyncCoalescer(lambda b: os.write(master_fd, b))

    def cleanup():
        server.close()
        for c in clients:
            try: c['sock'].close()
            except: pass
        try: os.unlink(spath)
        except: pass
        try: os.unlink(pid_path(name))
        except: pass
        try: os.kill(child_pid, signal.SIGTERM)
        except: pass

    try:
        while True:
            # Check if child is still alive
            try:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
                if pid != 0:
                    break
            except ChildProcessError:
                break

            # Safety: if a sync frame never got its ESU within the window, flush it
            # so a stuck/interrupted frame can't freeze the display.
            clients = _broadcast(clients, client_sizes, sync.timeout_flush(time.monotonic()))

            fds = [master_fd, server] + clients + pending
            try:
                readable, _, _ = select.select(fds, [], [], 1.0)
            except (select.error, ValueError, OSError):
                # Remove bad fds
                clients = [c for c in clients if c.fileno() >= 0]
                pending = [c for c in pending if c.fileno() >= 0]
                continue

            for fd in readable:
                if fd == master_fd:
                    # PTY output → buffer + broadcast
                    try:
                        data = os.read(master_fd, 16384)
                    except OSError:
                        cleanup()
                        os._exit(0)
                    if not data:
                        cleanup()
                        os._exit(0)

                    output_buffer.extend(data)   # raw stream (replay-on-connect history)
                    if len(output_buffer) > BUFFER_SIZE:
                        output_buffer[:] = output_buffer[-BUFFER_SIZE:]

                    # Coalesce synchronized-output frames, then broadcast atomically.
                    out = sync.feed(data, time.monotonic())
                    clients = _broadcast(clients, client_sizes, out)

                elif fd == server:
                    # New connection — add to pending, wait for size header
                    try:
                        client, _ = server.accept()
                        client.setblocking(False)
                        pending.append(client)
                    except OSError:
                        pass

                elif fd in pending:
                    # Pending client — try to read 5-byte header:
                    # 4 bytes size (cols, rows) + 1 byte flag
                    # flag 0x00 = real client (send buffer)
                    # flag 0x01 = resize only (skip buffer, close)
                    # flag 0x02 = TAIL SNAPSHOT: send the last 64KB of the buffer, then
                    #             CLOSE. One-shot screen peek (working-label render,
                    #             waiting-prompt detection). EOF tells the reader "you
                    #             have the complete current tail" — deterministic, no
                    #             read-for-N-seconds guessing — and the daemon never
                    #             flushes multi-MB history for a 700-char peek.
                    # flag 0x04 = LIVE-ONLY: skip the history replay, stream live output
                    #             only. Used by the working-state activity monitor: with
                    #             no history on the wire, every byte it sees is genuinely
                    #             live output, so a replay can NEVER be mistaken for work
                    #             (that mix-up was the phantom "working" spinner). We ACK
                    #             with 0x06 so the client knows the flag was honoured.
                    #             It also spares the daemon a blocking multi-MB sendall.
                    try:
                        header = fd.recv(5)
                        if len(header) >= 4:
                            cols, rows = struct.unpack('!HH', header[:4])
                            resize_only = len(header) == 5 and header[4] == 0x01
                            tail_snap = len(header) == 5 and header[4] == 0x02
                            live_only = len(header) == 5 and header[4] == 0x04
                            pending.remove(fd)
                            if resize_only:
                                # Legacy one-shot resize (kept for compat). Resizes
                                # now ride in-band on the persistent channel.
                                if cols > 0 and rows > 0:
                                    set_pty_size(master_fd, cols, rows)
                                fd.close()
                            elif tail_snap:
                                fd.setblocking(True)
                                fd.settimeout(5.0)
                                try:
                                    fd.sendall(bytes(output_buffer[-65536:]))
                                except:
                                    pass
                                fd.close()   # EOF = snapshot complete
                            else:
                                fd.setblocking(True)
                                fd.settimeout(5.0)
                                try:
                                    fd.sendall(b'\x06' if live_only else bytes(output_buffer))
                                except:
                                    fd.close()
                                    continue
                                clients.append(fd)
                                client_sizes[fd.fileno()] = (cols, rows)
                                # Opening a terminal = activity → this client governs
                                # the size now (tmux: most-recent client wins; other
                                # viewers letterbox instead of fighting over width).
                                if cols > 0 and rows > 0:
                                    active_fd[0] = fd.fileno()
                                    set_pty_size(master_fd, cols, rows)
                        else:
                            fd.close()
                            pending.remove(fd)
                    except (BlockingIOError, OSError):
                        pass
                    except:
                        try: fd.close()
                        except: pass
                        if fd in pending:
                            pending.remove(fd)

                else:
                    # Client input → PTY
                    if fd not in clients:
                        continue
                    try:
                        data = fd.recv(4096)
                        if not data:
                            raise ConnectionError
                        # Reassemble a GCSZ resize marker split across reads (prepend
                        # any held partial, hold a new trailing partial) — else its
                        # payload ("GCSZ;133;42") leaks into Claude's input line.
                        data = client_input_hold.pop(fd.fileno(), b'') + data
                        data, _partial = _hold_partial_resize(data)
                        # If we're holding a partial marker, drain whatever the kernel
                        # already split off into the socket buffer (the marker's tail
                        # arrives in the SAME write, so it's waiting NOW). Reassemble
                        # until complete or nothing more is available. A held lone ESC
                        # with nothing behind it is a real interrupt keypress → flush it.
                        while _partial:
                            r, _, _ = select.select([fd], [], [], 0)
                            if not r:
                                data += _partial       # nothing waiting → real ESC, flush now
                                _partial = b''
                                break
                            more = fd.recv(4096)
                            if not more:
                                data += _partial
                                _partial = b''
                                break
                            chunk, _partial = _hold_partial_resize(_partial + more)
                            chunk, _rz = _extract_resize(chunk)
                            if _rz is not None:
                                client_sizes[fd.fileno()] = _rz
                                if active_fd[0] == fd.fileno():
                                    set_pty_size(master_fd, _rz[0], _rz[1])
                            data += chunk
                        if _partial:
                            client_input_hold[fd.fileno()] = _partial
                        # In-band resize rides this client's own channel, so we know
                        # it's THIS client's size.
                        data, rz = _extract_resize(data)
                        if rz is not None:
                            client_sizes[fd.fileno()] = rz
                            # A background viewer's layout resize must NOT hijack the
                            # size — apply only if this is the active client.
                            if active_fd[0] == fd.fileno():
                                set_pty_size(master_fd, rz[0], rz[1])
                        if data:
                            # Real keystrokes = activity → this client becomes active
                            # (tmux-style) and the PTY adopts its size.
                            if active_fd[0] != fd.fileno():
                                active_fd[0] = fd.fileno()
                                sz = client_sizes.get(fd.fileno())
                                if sz and sz[0] > 0 and sz[1] > 0:
                                    set_pty_size(master_fd, sz[0], sz[1])
                            os.write(master_fd, data)
                    except:
                        try:
                            fno = fd.fileno()
                            client_sizes.pop(fno, None)
                            client_input_hold.pop(fno, None)
                        except:
                            fno = None
                        try: fd.close()
                        except: pass
                        if fd in clients:
                            clients.remove(fd)
                        # If the ACTIVE client left, hand the size crown to another
                        # attached viewer so the PTY isn't stuck at a gone client's size.
                        if fno is not None and active_fd[0] == fno:
                            active_fd[0] = None
                            for c in list(clients):
                                try: cf = c.fileno()
                                except: continue
                                sz = client_sizes.get(cf)
                                if sz and sz[0] > 0 and sz[1] > 0:
                                    active_fd[0] = cf
                                    set_pty_size(master_fd, sz[0], sz[1])
                                    break
    except:
        pass
    finally:
        cleanup()
        os._exit(0)


def set_pty_size(fd, cols, rows):
    try:
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except:
        pass


class _SyncCoalescer:
    """Make EZ synchronized-output (DEC private mode 2026) aware.

    Claude Code only wraps each redraw in ESC[?2026h .. ESC[?2026l when the
    terminal advertises mode-2026 support. Our clients (xterm.js v5, older
    SwiftTerm) don't, so Claude leaves sync OFF and every partial in-place redraw
    (cursor-up, clear, rewrite) is flushed live → renderer-independent flicker on
    the small mobile viewport, on every client. This coalescer (same approach as
    tmux PR #4744) sits in the byte stream and:
      1. answers Claude's DECRQM probe (ESC[?2026$p) with a positive reply so
         Claude turns synchronized output ON, and strips the probe so clients
         don't also answer it;
      2. buffers each ESC[?2026h..ESC[?2026l frame and emits it as ONE atomic
         chunk, so the whole redraw lands in a single client paint = no flicker.
    A safety timeout flushes a frame whose ESU never arrives, so a stuck or
    interrupted frame can't freeze the display.
    """
    BSU = b'\x1b[?2026h'          # begin synchronized update
    ESU = b'\x1b[?2026l'          # end synchronized update
    DECRQM = b'\x1b[?2026$p'      # app asks: do you support mode 2026?
    DECRPM = b'\x1b[?2026;2$y'    # our reply: yes (supported, currently reset)
    TIMEOUT = 1.0                 # flush a stuck frame after this (matches xterm/tmux)

    def __init__(self, reply_writer):
        self._reply = reply_writer   # called with the DECRQM answer (→ app stdin)
        self.active = False
        self.deadline = 0.0
        self.frame = bytearray()     # frame being coalesced
        self.hold = bytearray()      # withheld bytes: a marker split across reads

    def _partial_tail_len(self, buf):
        # Longest suffix of buf that is a prefix of BSU or DECRQM, so a control
        # marker split across two PTY reads isn't missed. Only short control
        # prefixes are ever withheld — normal text always flows straight through.
        maxk = min(len(buf), max(len(self.BSU), len(self.DECRQM)) - 1)
        for k in range(maxk, 0, -1):
            tail = buf[-k:]
            if self.BSU.startswith(tail) or self.DECRQM.startswith(tail):
                return k
        return 0

    def feed(self, data, now):
        """Consume raw PTY output; answer/strip the DECRQM probe; coalesce each
        h..l frame. Returns the bytes to broadcast right now (b'' while mid-frame)."""
        out = bytearray()
        buf = bytes(self.hold) + data
        self.hold.clear()
        while buf:
            if self.active:
                self.frame.extend(buf)
                buf = b''
                idx = self.frame.find(self.ESU)
                if idx != -1:
                    end = idx + len(self.ESU)
                    out.extend(self.frame[:end])     # whole frame → one flush
                    buf = bytes(self.frame[end:])    # bytes after ESU = fresh
                    self.frame.clear()
                    self.active = False
                # else: still buffering the frame; wait for more data / timeout
            else:
                q = buf.find(self.DECRQM)
                if q != -1:
                    self._reply(self.DECRPM)         # tell Claude: sync supported
                    buf = buf[:q] + buf[q + len(self.DECRQM):]  # strip so clients don't also reply
                    continue
                b = buf.find(self.BSU)
                if b != -1:
                    out.extend(buf[:b])              # everything before the frame goes now
                    self.active = True
                    self.deadline = now + self.TIMEOUT
                    self.frame.clear()
                    buf = buf[b:]                    # re-enter loop in the sync branch
                    continue
                t = self._partial_tail_len(buf)      # no marker: emit all but a split-marker tail
                if t:
                    out.extend(buf[:-t])
                    self.hold.extend(buf[-t:])
                else:
                    out.extend(buf)
                buf = b''
        return bytes(out)

    def timeout_flush(self, now):
        """Flush a frame whose ESU never arrived within TIMEOUT. Returns bytes to
        broadcast (b'' if nothing to flush)."""
        if self.active and now >= self.deadline:
            out = bytes(self.frame)
            self.frame.clear()
            self.active = False
            return out
        return b''


def _broadcast(clients, client_sizes, data):
    """Send `data` to every client in ONE sendall each (atomic per client), and
    reap any that died. Returns the (possibly shrunk) clients list. Empty data =
    no-op. Single sendall is what keeps a coalesced sync frame from tearing."""
    if not data:
        return clients
    dead = []
    for i, client in enumerate(clients):
        try:
            client.sendall(data)
        except (BrokenPipeError, OSError):
            dead.append(i)
    for i in reversed(dead):
        try:
            client_sizes.pop(clients[i].fileno(), None)
            clients[i].close()
        except:
            pass
        clients.pop(i)
    return clients


# In-band resize control that rides a client's persistent input channel, so the
# daemon knows WHICH client resized (ESC _ GCSZ;cols;rows ESC \ — an APC string
# real terminal input never emits). This is what lets the size follow the ACTIVE
# client (tmux-style) instead of whoever's one-shot resize landed last.
_RESIZE_RE = re.compile(rb'\x1b_GCSZ;(\d+);(\d+)\x1b\\')
_RESIZE_PREFIX = b'\x1b_GCSZ'


def _hold_partial_resize(data):
    """If `data` ends with an INCOMPLETE GCSZ resize APC (`\\x1b_GCSZ...` with no
    closing `\\x1b\\` yet — the marker got split across socket reads), split it off
    so it can be reassembled with the next read instead of leaking its payload into
    Claude's input line. Returns (data_without_partial, held_partial)."""
    i = data.rfind(b'\x1b_')
    if i != -1:
        seg = data[i:]
        if b'\x1b\\' in seg:
            return data, b''                   # complete APC present — regex handles it
        if _RESIZE_PREFIX.startswith(seg) or seg.startswith(_RESIZE_PREFIX):
            return data[:i], seg               # (a prefix of) our marker — hold it
        return data, b''                       # unrelated APC — leave it alone
    # No '\x1b_' yet, but a lone trailing ESC can be the split-off START of our marker
    # ('\x1b' | '_GCSZ;...') — the single case that still leaked. Hold it; the caller
    # drains any immediately-waiting tail to reassemble, and flushes it (a real ESC
    # keypress / interrupt) if nothing follows — so interrupts are never delayed.
    if data.endswith(b'\x1b'):
        return data[:-1], b'\x1b'
    return data, b''


def _extract_resize(data):
    """Pull the LAST in-band resize control out of `data`. Returns (clean_bytes,
    (cols, rows) | None)."""
    matches = list(_RESIZE_RE.finditer(data))
    if not matches:
        return data, None
    m = matches[-1]
    clean = _RESIZE_RE.sub(b'', data)
    try:
        return clean, (int(m.group(1)), int(m.group(2)))
    except ValueError:
        return clean, None


# ============================================================
# CLIENT MODE — connects to daemon, bridges stdin/stdout
# ============================================================

def attach(name):
    """Attach to an existing session."""
    spath = socket_path(name)
    if not os.path.exists(spath):
        print(f"eztmux: no session '{name}'", file=sys.stderr)
        print(f"  Start one with: eztmux start {name}", file=sys.stderr)
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(spath)
    except (ConnectionRefusedError, FileNotFoundError):
        try: os.unlink(spath)
        except: pass
        print(f"eztmux: session '{name}' is dead (cleaned up)", file=sys.stderr)
        sys.exit(1)

    # Send terminal size + flag (0x00 = real client, send buffer)
    cols, rows = get_terminal_size()
    sock.sendall(struct.pack('!HH', cols, rows) + b'\x00')
    last_size = (cols, rows)

    # Set terminal title so the app can show a session banner
    sys.stdout.buffer.write(f"\033]0;eztmux:{name}\007".encode())
    sys.stdout.buffer.flush()

    # Set raw mode
    old_settings = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())

    def send_resize(c, r):
        """Send a resize to the daemon via a new connection (flag 0x01 = resize only)."""
        try:
            rs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            rs.connect(spath)
            rs.sendall(struct.pack('!HH', c, r) + b'\x01')
            rs.close()
        except:
            pass

    # Handle SIGWINCH
    def on_resize(signum, frame):
        nonlocal last_size
        c, r = get_terminal_size()
        if (c, r) != last_size:
            last_size = (c, r)
            send_resize(c, r)

    signal.signal(signal.SIGWINCH, on_resize)

    last_resize_time = [0.0]

    def maybe_send_resize():
        """Re-send our size if >2s since last resize. Guarantees active device wins."""
        now = time.time()
        if now - last_resize_time[0] > 2.0:
            last_resize_time[0] = now
            c, r = get_terminal_size()
            send_resize(c, r)

    try:
        while True:
            try:
                readable, _, _ = select.select([sys.stdin, sock], [], [], 0.5)
            except (select.error, InterruptedError):
                continue

            # Poll for size changes (catches resizes that SIGWINCH misses)
            current_size = get_terminal_size()
            if current_size != last_size:
                last_size = current_size
                send_resize(*current_size)
                last_resize_time[0] = time.time()

            if sys.stdin in readable:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    sock.sendall(data)
                    # Re-send our size while actively typing
                    maybe_send_resize()
                except:
                    break

            if sock in readable:
                try:
                    data = sock.recv(16384)
                    if not data:
                        print("\r\n[session ended]\r\n", end='')
                        break
                    os.write(sys.stdout.fileno(), data)
                except:
                    break
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sock.close()


def get_terminal_size():
    try:
        data = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', data)[:2]
        return cols, rows
    except:
        return 80, 24


# ============================================================
# MANAGEMENT COMMANDS
# ============================================================

def list_sessions():
    os.makedirs(SOCKET_DIR, exist_ok=True)
    sessions = [f[:-5] for f in os.listdir(SOCKET_DIR) if f.endswith('.sock')]
    if not sessions:
        print("no active sessions")
        return
    for name in sorted(sessions):
        status = "alive" if is_session_alive(name) else "dead"
        print(f"  {name} ({status})")
        if status == "dead":
            try: os.unlink(socket_path(name))
            except: pass
            try: os.unlink(pid_path(name))
            except: pass


def kill_session(name):
    ppath = pid_path(name)
    if os.path.exists(ppath):
        try:
            with open(ppath) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"killed session '{name}'")
        except:
            print(f"could not kill session '{name}'", file=sys.stderr)
    try: os.unlink(socket_path(name))
    except: pass
    try: os.unlink(ppath)
    except: pass


# ============================================================
# CLI
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'ls':
        list_sessions()
    elif cmd == 'kill' and len(sys.argv) >= 3:
        kill_session(sys.argv[2])
    elif cmd == 'start' and len(sys.argv) >= 3:
        name = sys.argv[2]
        command = sys.argv[3:] if len(sys.argv) > 3 else [os.environ.get('SHELL', 'bash'), '-l']
        daemon_main(name, command)
        attach(name)
    elif cmd == 'attach' and len(sys.argv) >= 3:
        attach(sys.argv[2])
    elif cmd in ('--help', '-h', 'help'):
        print(__doc__.strip())
    else:
        # Shorthand: eztmux <name> [command...] = create-or-attach
        name = cmd
        command = sys.argv[2:] if len(sys.argv) > 2 else [os.environ.get('SHELL', 'bash'), '-l']
        spath = socket_path(name)
        if os.path.exists(spath) and is_session_alive(name):
            attach(name)
        else:
            # Clean up stale files
            try: os.unlink(spath)
            except: pass
            try: os.unlink(pid_path(name))
            except: pass
            daemon_main(name, command)
            attach(name)


if __name__ == '__main__':
    main()
