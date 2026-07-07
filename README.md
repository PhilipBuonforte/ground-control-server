# Ground Control — Mac Server

This little server runs on your Mac and lets the **Ground Control** iOS app see and
control your Claude Code sessions from your phone, anywhere.

## Install (one command)

Open **Terminal** on your Mac, then paste:

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipBuonforte/ground-control-server/main/install.sh | bash
```

The installer will:
1. Check for Python 3 and Claude Code
2. Install the server to `~/.ground-control`
3. Set it to auto-start and stay running (survives reboots)
4. Turn on secure remote access via Tailscale (HTTPS)
5. Print your server address to paste into the app

## What you need

- **macOS** with **Claude Code** (claude.ai/code) installed and a few sessions
- **Tailscale** (free) on both your Mac and iPhone — https://tailscale.com/download
  - Sign in with the same account on both; keep the VPN toggle ON on the iPhone
- The **Ground Control** app from TestFlight

## After install

1. Open **Ground Control** on your iPhone
2. Paste the server address the installer printed (looks like
   `https://your-mac.your-tailnet.ts.net`)
3. Tap **Test & Connect**

## Managing the server

- **Logs:** `~/.ground-control/server.log` and `server.err`
- **Restart:** `launchctl unload ~/Library/LaunchAgents/com.groundcontrol.server.plist && launchctl load ~/Library/LaunchAgents/com.groundcontrol.server.plist`
- **Uninstall:** `launchctl unload ~/Library/LaunchAgents/com.groundcontrol.server.plist && rm -rf ~/.ground-control ~/Library/LaunchAgents/com.groundcontrol.server.plist`

## Note on push notifications

Live push alerts (buzz your phone when a session finishes) are enabled for the
primary developer build. In this early TestFlight, the in-app session list,
messaging, attachments, and working-status all work for everyone; broader
push support is coming.

## Privacy

Your conversations never leave your own devices. The app talks directly to
**your** Mac over **your** private Tailscale network — messages, files, and
session content are never sent to us or anyone else.

The only thing that touches Ground Control's servers is the notification
ping: your Mac asks our push relay to buzz your phone with the **session
name only** (e.g. "✅ My Project — Tap to view"). No message content is ever
included, and nothing is stored.
