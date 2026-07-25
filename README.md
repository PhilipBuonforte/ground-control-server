# Ground Control — run your Claude Code sessions from anywhere

Ground Control turns your Mac's Claude Code sessions into something you can watch and
drive from your **iPhone** and a native **Mac app** — live terminals, chat view, smart
alerts when a session finishes or needs an answer, and one-tap replies to Claude's
questions.

Your Mac does the work; the apps are windows into it, connected privately over
[Tailscale](https://tailscale.com) (free).

## Setup (about 3 minutes — no Terminal)

### 1. Download and open the Mac app

[**Download Ground Control.app**](https://github.com/PhilipBuonforte/ground-control-server/releases/latest/download/GroundControl-mac.zip)
— unzip, drag into Applications, open it.
(First open: **right-click the app → Open → Open** to pass macOS's unsigned-app check.)

### 2. Click "Set Up This Mac"

That one button installs everything — the server, auto-start, and Claude Code's
alert hooks. About a minute. When it finishes it shows your server address.

### 3. iPhone

- Install Tailscale (free) on your **Mac and iPhone**, signed into the **same
  account** on both: https://tailscale.com/download
- iPhone app: TestFlight → TESTFLIGHT_LINK_HERE — paste your server address.

**Updates are automatic**: the Mac app shows an "Update available → Install" banner
(one click updates the app AND the server); the iPhone updates through TestFlight.

<details>
<summary>Alternative: command-line install</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipBuonforte/ground-control-server/main/install.sh | bash
```
Does the same thing, including installing the Mac app.
</details>

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

- **"Apple could not verify" on open** (only if you downloaded the app manually) →
  System Settings → Privacy & Security → Open Anyway, or:
  `xattr -dr com.apple.quarantine "/Applications/Ground Control.app"`
- **App can't connect** → is Tailscale signed in and toggled ON on the iPhone?
- **No address printed** → sign into Tailscale on the Mac, re-run the installer

## Privacy

Your conversations never leave your own devices. The apps talk directly to **your**
Mac over **your** private Tailscale network — messages, files, and session content
are never sent to us or anyone else.

The only thing that touches Ground Control's servers is the notification ping: your
Mac asks our push relay to buzz your phone with the **session name only** (e.g.
"✅ My Project — Tap to view"). No message content is ever included, nothing is stored.
