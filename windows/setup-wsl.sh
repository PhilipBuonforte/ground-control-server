#!/usr/bin/env bash
# setup-wsl.sh — install the Ground Control server on a Windows PC (inside WSL2),
# or on any plain Linux box. Idempotent: safe to re-run after a failure.
#
#   bash setup-wsl.sh            # install
#   bash setup-wsl.sh --check    # dry run: inspect the machine, change nothing
#
# WHY WSL AND NOT A NATIVE WINDOWS BUILD
# The engine is a raw PTY: gc_ez_engine.py uses os.openpty, os.fork, AF_UNIX sockets,
# termios/fcntl and SIGWINCH. Every one of those is native on Linux, so the server runs
# UNMODIFIED here. On native Windows none of them exist — a port means replacing that
# layer with ConPTY (pywinpty), threads instead of fork and loopback TCP instead of unix
# sockets: days of work on the riskiest file in the project, for no gain unless you are
# shipping to users who refuse WSL.
#
# Audited 2026-11-12: server.py's top-level imports are stdlib + FastAPI only and it
# imports cleanly (129 routes). Its sole macOS references are two Path definitions
# (Library/Messages/chat.db and the AddressBook glob); a Path to a missing file is
# harmless, so the iMessage features simply stay dark.
set -uo pipefail

REPO="https://github.com/PhilipBuonforte/ground-control-server.git"
DIR="$HOME/ground-control"
PORT=8130
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

B=$'\e[34m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
say(){ echo "${B}▸${N} $1"; }
ok(){  echo "${G}✓${N} $1"; }
warn(){ echo "${Y}!${N} $1"; }
die(){ echo "${R}✗ $1${N}"; exit 1; }
would(){ echo "  ${Y}would:${N} $1"; }

echo
echo "=== Ground Control server — Linux/WSL setup$([ $CHECK = 1 ] && echo '  [DRY RUN]') ==="
echo

# ---- 0. Where are we? ----------------------------------------------------
IS_WSL=0
grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
if [ "$IS_WSL" = 1 ]; then
  ok "WSL2 detected ($(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}"))"
elif [ "$(uname -s)" = "Linux" ]; then
  warn "not WSL — plain Linux. That is fine, everything below still applies."
else
  die "This must run on Linux. On Windows: open PowerShell, run 'wsl --install -d Ubuntu', reboot, then run this inside Ubuntu."
fi
command -v apt-get >/dev/null 2>&1 || die "No apt-get. This script targets Debian/Ubuntu; install python3/venv/git/curl yourself, then re-run."

# ---- 1. System packages --------------------------------------------------
say "Checking system packages…"
NEED=""
for p in python3 git curl; do command -v "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
for p in python3-venv python3-pip; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
if [ -n "$NEED" ]; then
  if [ $CHECK = 1 ]; then would "sudo apt-get install -y$NEED"
  else
    say "installing:$NEED (sudo will ask for your Linux password)"
    sudo apt-get update -qq || die "apt-get update failed — does WSL have network? try: curl -I https://github.com"
    sudo apt-get install -y -qq $NEED || die "apt-get install failed:$NEED"
  fi
else
  ok "python3 / git / curl / venv all present"
fi
command -v python3 >/dev/null 2>&1 && ok "python $(python3 -V 2>&1 | awk '{print $2}')"

# ---- 2. Claude Code ------------------------------------------------------
say "Checking for Claude Code…"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
  ok "claude present"
elif [ $CHECK = 1 ]; then
  would "curl -fsSL https://claude.ai/install.sh | bash"
else
  warn "not installed in this Linux environment — installing…"
  curl -fsSL https://claude.ai/install.sh | bash || warn "installer failed; install Claude Code manually and re-run"
  export PATH="$HOME/.local/bin:$PATH"
fi

# ---- 3. Server files -----------------------------------------------------
if [ $CHECK = 1 ]; then
  [ -d "$DIR/.git" ] && would "git -C $DIR pull" || would "git clone $REPO $DIR"
elif [ -d "$DIR/.git" ]; then
  say "Updating $DIR…"
  git -C "$DIR" pull --ff-only -q || warn "git pull failed — keeping what is on disk"
else
  say "Cloning to $DIR…"
  git clone -q "$REPO" "$DIR" || die "git clone failed — check network/access to $REPO"
fi
if [ $CHECK = 0 ]; then
  [ -f "$DIR/server.py" ] || die "server.py missing in $DIR"
  [ -d "$DIR/ezterminfo" ] || warn "ezterminfo/ missing — terminal rendering may be off"
  ok "server files in place"
fi

# ---- 4. Python environment ----------------------------------------------
if [ $CHECK = 1 ]; then
  would "python3 -m venv $DIR/venv && pip install -r requirements.txt"
else
  say "Installing Python dependencies (first run takes a minute)…"
  [ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv" || die "could not create venv (is python3-venv installed?)"
  "$DIR/venv/bin/python3" -m pip install -q --upgrade pip
  "$DIR/venv/bin/python3" -m pip install -q -r "$DIR/requirements.txt" \
    || die "pip install failed. Run it by hand to see why: $DIR/venv/bin/python3 -m pip install -r $DIR/requirements.txt"
  ok "dependencies installed"

  # Proves the macOS-only bits degrade instead of crashing. NOTE: the exit status of a
  # pipeline is its LAST command, so piping python into `tail` would make this check
  # always pass. Capture the status explicitly.
  say "Import check…"
  IMPORT_OUT=$(cd "$DIR" && "$DIR/venv/bin/python3" -c "import server; print(len(server.app.routes))" 2>&1)
  IMPORT_RC=$?
  if [ $IMPORT_RC -eq 0 ]; then
    ok "server.py imports cleanly — $IMPORT_OUT routes"
  else
    echo "$IMPORT_OUT" | tail -15
    die "server.py failed to import (above). Paste that to Claude."
  fi
fi

# ---- 5. Alert hook ------------------------------------------------------
say "Registering the Claude Code alert hook…"
if [ $CHECK = 1 ]; then
  would "copy hooks/pocket-claude-notify.py to ~/.claude/hooks and register it in settings.json"
elif [ -f "$DIR/hooks/pocket-claude-notify.py" ]; then
  mkdir -p "$HOME/.claude/hooks"
  cp "$DIR/hooks/pocket-claude-notify.py" "$HOME/.claude/hooks/pocket-claude-notify.py"
  "$DIR/venv/bin/python3" - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
CMD = "python3 ~/.claude/hooks/pocket-claude-notify.py"
hooks = cfg.setdefault("hooks", {})
for event in ("Stop", "Notification", "SubagentStart", "SubagentStop"):
    e = hooks.setdefault(event, [])
    if not any(CMD in json.dumps(x) for x in e):
        e.append({"hooks": [{"type": "command", "command": CMD}]})
pre = hooks.setdefault("PreToolUse", [])
if not any(CMD in json.dumps(x) for x in pre):
    pre.append({"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": CMD}]})
# Auto-compact is a USER-level setting and it is what stops a long-lived session
# bricking on "Prompt is too long". Measured 2026-11-12: a session with it off reached
# 975,000 tokens and had to be cleared by hand.
cfg.setdefault("autoCompactEnabled", True)
tmp = p + ".tmp"
json.dump(cfg, open(tmp, "w"), indent=2)
os.replace(tmp, p)          # atomic: a half-written settings.json silently breaks alerts
print("  settings.json updated")
PY
  ok "alert hook registered"
else
  warn "hook not in the repo — alerts will not fire; everything else works"
fi

# ---- 6. Keep it running -------------------------------------------------
# Launched exactly as the Mac does it: `python3 server.py`, whose __main__ binds
# 0.0.0.0:8130. Same entrypoint = same startup behaviour.
say "Setting up auto-start…"
mkdir -p "$HOME/.ground-control"
if [ $CHECK = 1 ]; then
  [ -d /run/systemd/system ] && would "install systemd --user service 'ground-control' + enable-linger" \
                             || would "start with nohup (systemd is off in this distro)"
elif [ -d /run/systemd/system ]; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/ground-control.service" <<UNIT
[Unit]
Description=Ground Control server
After=network.target

[Service]
ExecStart=$DIR/venv/bin/python3 server.py
WorkingDirectory=$DIR
Restart=always
RestartSec=3
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now ground-control.service >/dev/null 2>&1
  sudo loginctl enable-linger "$USER" >/dev/null 2>&1 \
    || warn "could not enable linger — the server stops when you close the Ubuntu window"
  sleep 3
  systemctl --user is-active --quiet ground-control.service \
    && ok "systemd service running" \
    || warn "not active — check: systemctl --user status ground-control"
else
  warn "systemd is off in this distro. Turn it on (also what keeps the server alive"
  warn "after you close Ubuntu):"
  echo "      printf '[boot]\\nsystemd=true\\n' | sudo tee /etc/wsl.conf"
  echo "      then in PowerShell:  wsl --shutdown   (and reopen Ubuntu)"
  say "starting in the background for now…"
  pkill -f "$DIR/venv/bin/python3 server.py" 2>/dev/null   # narrow: only OUR server
  ( cd "$DIR" && nohup "$DIR/venv/bin/python3" server.py > "$HOME/.ground-control/server.log" 2>&1 & )
  sleep 4
fi

# ---- 7. Tailscale serve, same as the Mac --------------------------------
if [ $CHECK = 0 ] && command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  tailscale serve status 2>/dev/null | grep -q "$PORT" || tailscale serve --bg "$PORT" >/dev/null 2>&1 || true
  ok "tailscale serve on $PORT (matches run_server.sh on the Mac)"
fi

# ---- 8. Self-test -------------------------------------------------------
if [ $CHECK = 1 ]; then
  echo; ok "Dry run complete — nothing was changed. Re-run without --check to install."; exit 0
fi
echo
say "Self-test…"
HEALTH=$(curl -s -m 8 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
if echo "$HEALTH" | grep -q '"ok"'; then
  ok "server answering on $PORT  ->  $HEALTH"
else
  warn "nothing on port $PORT. Logs:"
  tail -20 "$HOME/.ground-control/server.log" 2>/dev/null
  [ -d /run/systemd/system ] && journalctl --user -u ground-control -n 20 --no-pager 2>/dev/null
  die "server did not come up — paste the above to Claude"
fi
[ -x "$DIR/gc-doctor" ] && { say "gc-doctor:"; "$DIR/gc-doctor" 2>&1 | tail -25; }

echo
ok "Ground Control is installed."
echo
echo "  On this PC:      http://localhost:$PORT/groundzero"
echo "  Live terminal:   http://localhost:$PORT/terminal"
echo
echo "  TWO THINGS ONLY YOU CAN DO:"
echo "   1. claude            # log in to Claude Code once"
echo "   2. reach it from your phone:"
echo "        curl -fsSL https://tailscale.com/install.sh | sh"
echo "        sudo tailscale up      # open the link it prints"
echo "        tailscale ip -4        # put http://<ip>:$PORT in the phone app"
echo
echo "  The server listens on 0.0.0.0 and has NO authentication (same as the Mac),"
echo "  so reach it over Tailscale — never expose port $PORT to an untrusted network."
echo
