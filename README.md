# Ground Control — run your Claude Code sessions from anywhere

Ground Control turns your Mac's Claude Code sessions into something you can watch and
drive from your **iPhone** and a native **Mac app** — live terminals, chat view, smart
alerts when a session finishes or needs an answer, and one-tap replies to Claude's
questions.

Your Mac does the work; the apps are windows into it, connected privately over
[Tailscale](https://tailscale.com) (free).

## Setup (about 5 minutes)

### 1. Install the server on your Mac

Open **Terminal** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipBuonforte/ground-control-server/main/install.sh | bash
```

This installs the server to `~/.ground-control`, keeps it running (survives reboots),
and wires up Claude Code's alert hooks.

### 2. Connect your devices with Tailscale (free)

1. Install Tailscale on your **Mac** and **iPhone**: https://tailscale.com/download
2. Sign in with the **same account** on both (Google/Apple login works); keep the
   VPN toggle ON on the iPhone
3. Re-run the installer command — it finishes the secure HTTPS route and
   **prints your server address** (like `https://your-mac.your-tailnet.ts.net`)

### 3. Install the apps

- **iPhone**: TestFlight → TESTFLIGHT_LINK_HERE
- **Mac**: download **Ground Control.app** from the
  [latest release](https://github.com/PhilipBuonforte/ground-control-server/releases/latest),
  unzip, drag into `/Applications`.
  First launch: macOS may warn it's from an unidentified developer — go to
  **System Settings → Privacy & Security → Open Anyway** (one time only).

Open either app, paste your server address from step 2, and you're in.

## What you need

- macOS with [Claude Code](https://claude.ai/code) installed
- An iPhone (iOS 17+)
- A free Tailscale account

## What you get

- **Live terminal on your phone** — the real session, not a replica
- **New sessions from anywhere** — name it, pick a folder, type the first message
- **Alerts that don't lie** — buzz when a session truly finishes or is stuck on a
  question; acknowledge on one device, the banner clears on the other
- **Tap to answer** — Claude's questions, permission prompts, and menus become
  tappable buttons in the app
- **Attach images** — drop a screenshot into chat or terminal; Claude can read it

## Managing the server

- **Status:** `curl http://127.0.0.1:8130/api/health`
- **Logs:** `~/.ground-control/server.log` and `server.err`
- **Restart:** `launchctl unload ~/Library/LaunchAgents/com.groundcontrol.server.plist && launchctl load ~/Library/LaunchAgents/com.groundcontrol.server.plist`
- **Uninstall:** `launchctl unload ~/Library/LaunchAgents/com.groundcontrol.server.plist && rm -rf ~/.ground-control ~/Library/LaunchAgents/com.groundcontrol.server.plist`

## Troubleshooting

- **App can't connect** → is Tailscale signed in and toggled ON on the iPhone?
- **No address printed** → sign into Tailscale on the Mac, re-run the installer

## Privacy

Your conversations never leave your own devices. The apps talk directly to **your**
Mac over **your** private Tailscale network — messages, files, and session content
are never sent to us or anyone else.

The only thing that touches Ground Control's servers is the notification ping: your
Mac asks our push relay to buzz your phone with the **session name only** (e.g.
"✅ My Project — Tap to view"). No message content is ever included, nothing is stored.
