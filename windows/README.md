# Ground Control on Windows

The server runs on a Windows PC **inside WSL2**, Microsoft's real Linux environment.
This is the supported route, not a workaround.

## Why not a native Windows build

The terminal engine is a raw PTY. `gc_ez_engine.py` uses `os.openpty`, `os.fork`,
`AF_UNIX` sockets, `termios`/`fcntl` and `SIGWINCH`. On Linux those are all native —
which is why the server runs here **unmodified**. On native Windows none of them
exist: a port means replacing the PTY layer with ConPTY (`pywinpty`), threads instead
of `fork`, and loopback TCP instead of unix sockets. That is days of work on the
riskiest file in the project, and it buys nothing unless you need to ship to users who
refuse WSL.

Audited 2026-11-12: `server.py`'s top-level imports are stdlib + FastAPI only, and its
only macOS references are two `Path` definitions (`Library/Messages/chat.db` and the
AddressBook glob). A path to a file that does not exist is harmless, so it imports
clean on Linux and those features simply stay dark.

## What works, and what doesn't

| | Windows (WSL2) |
|---|---|
| EZ terminal engine, sessions, web terminal | works |
| Phone app, push alerts via the relay | works |
| Ground Zero + usage + context rings | works |
| iMessage / Text Assistant | **no** — macOS only (`chat.db`) |
| Screen capture (`gc_screen.py`) | **no** — uses `osascript`/`sips` |
| Native desktop app | **no** — the Mac app is SwiftUI; use the browser |

The UI on Windows is the browser: `/groundzero` and `/terminal`, plus the phone app.

## Install

**1. In PowerShell as Administrator** — installs WSL2 + Ubuntu, then reboot:

```powershell
wsl --install -d Ubuntu
```

After the reboot Ubuntu opens and asks for a username and password. That password is
for Linux `sudo`, not your Microsoft account.

**2. In the Ubuntu shell:**

```bash
git clone https://github.com/PhilipBuonforte/ground-control-server.git ~/gc-src
bash ~/gc-src/windows/setup-wsl.sh --check   # dry run: inspects, changes nothing
bash ~/gc-src/windows/setup-wsl.sh           # then install for real
```

Run `--check` first on a new machine. It reports what is missing and what it would do
without touching anything, so a bad surprise costs you nothing. The real run is
idempotent too — safe to re-run after any failure.

It also works on plain Linux, not just WSL; the only WSL-specific parts are the
systemd-in-`/etc/wsl.conf` advice and the `wsl --shutdown` step.

**3. Two things the script can't do for you:**

```bash
claude                                        # log in to Claude Code once
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                             # open the link it prints
tailscale ip -4                               # use http://<ip>:8130 in the phone app
```

## Running it

With systemd enabled (the script sets this up):

```bash
systemctl --user status ground-control
systemctl --user restart ground-control
journalctl --user -u ground-control -f        # logs
```

If systemd is off in your distro, switch it on once — this is also what keeps the
server alive after you close the Ubuntu window:

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
# then in PowerShell:  wsl --shutdown     (and reopen Ubuntu)
```

## Gotchas worth knowing up front

- **Keep your projects inside WSL** (`~/code/...`), not on `/mnt/c/...`. Crossing the
  Windows filesystem boundary is slow enough to be noticeable on every file read.
- **WSL stops when Windows sleeps.** `loginctl enable-linger` (the script does it)
  keeps the server up while Windows is awake with no shell open, but a sleeping laptop
  is a stopped server — the same constraint the Mac has.
- **Run Tailscale inside WSL**, not on the Windows host. WSL2 sits behind a NAT, so
  host-side Tailscale does not expose port 8130 from the distro without extra
  forwarding. Tailscale in the distro just works and appears as its own tailnet node.

## Security note

The server listens on `0.0.0.0:8130` and has **no authentication** — this matches the
Mac (`server.py`'s `__main__` binds `0.0.0.0`, and `run_server.sh` exposes it with
`tailscale serve`). Anyone who can reach that port can start sessions and run commands
on the machine. So reach it over **Tailscale**, and never expose port 8130 to a network
you do not control — a coffee-shop wifi is exactly the wrong place for it.
