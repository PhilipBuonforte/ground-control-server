#!/bin/bash
# Ground Control server launcher (used by launchd on Phil's Mac AND by public
# installs at ~/.ground-control — it resolves everything from its own location,
# no hardcoded paths).
#
# Guarantees THIS instance owns port 8130. A leftover manual `python server.py`
# squatting on the port once made notifications silently vanish (it kept saving
# the device token then deleting it on every failed push while the managed
# service couldn't bind). Killing any squatter first makes that impossible.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# Pick the python: an install-local venv wins (public installs create one so we
# never fight macOS's externally-managed system python), then Homebrew, then system.
if [ -x "$DIR/venv/bin/python3" ]; then
  PY="$DIR/venv/bin/python3"
elif [ -x /opt/homebrew/bin/python3 ]; then
  PY=/opt/homebrew/bin/python3
else
  PY="$(command -v python3 || echo /usr/bin/python3)"
fi

# Kill anything already bound to 8130 (a stale/orphan server), then let it free up.
for pid in $(lsof -ti tcp:8130 2>/dev/null); do
  kill -9 "$pid" 2>/dev/null
done
sleep 1

# Ensure deps — pyte is the working-state renderer; fastapi/uvicorn/httpx are the
# server itself. Cheap no-op when already installed. (Venv installs never need
# --break-system-packages; the Homebrew/system fallback does.)
if ! "$PY" -c "import pyte, fastapi, uvicorn, httpx" 2>/dev/null; then
  "$PY" -m pip install -q pyte fastapi uvicorn httpx 2>/dev/null || \
    "$PY" -m pip install --break-system-packages -q pyte fastapi uvicorn httpx 2>/dev/null || true
fi

# Self-heal remote access: if Tailscale is present and logged in but its HTTPS
# route to this server is gone (reboot, network change — it happens), restore it.
# Non-fatal: no Tailscale just means local-only until the user sets it up.
TS=""
for c in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then TS="$c"; break; fi
done
if [ -n "$TS" ] && "$TS" status >/dev/null 2>&1; then
  "$TS" serve status 2>/dev/null | grep -q "8130" || \
    "$TS" serve --bg 8130 >/dev/null 2>&1 || true
fi

exec "$PY" server.py
