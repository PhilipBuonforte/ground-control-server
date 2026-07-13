#!/usr/bin/env python3
"""Pocket Claude alert hook.

Fires on Stop (session finished a turn) and Notification (needs input/permission).
Reads the hook JSON on stdin, extracts session + last message, and pings the
Pocket Claude server, which sends a web-push to Phil's phone.
"""
import json
import sys
import urllib.request

SERVER = "http://127.0.0.1:8130/api/session-event"
SUBAGENT_SERVER = "http://127.0.0.1:8130/api/subagent-event"


def _post(url, obj):
    try:
        req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass


def last_assistant_text(transcript_path):
    text = None
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant" and not d.get("isSidechain"):
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                        t = "\n".join(p for p in parts if p).strip()
                        if t:
                            text = t
                    elif isinstance(c, str) and c.strip():
                        text = c.strip()
    except OSError:
        pass
    return text


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    transcript = data.get("transcript_path", "")
    cwd = data.get("cwd", "")

    # Background-agent lifecycle → tell the server so the session reads "working"
    # while a subagent runs even when the main prompt is idle. (No alert here.)
    if event in ("SubagentStart", "SubagentStop"):
        # Key by the TRANSCRIPT filename (== the canonical session id / EZ name the
        # app + is_working use). The hook's own session_id can differ for resumed /
        # background-job sessions, which would file the count under the wrong key.
        tid = transcript.rstrip("/").split("/")[-1] if transcript else ""
        if tid.endswith(".jsonl"):
            tid = tid[:-6]
        _post(SUBAGENT_SERVER, {
            "event": event,
            "session_id": tid or session_id,
            "raw_session_id": session_id,
            "agent_id": data.get("agent_id", ""),
            "agent_type": data.get("agent_type", ""),
        })
        return

    proj = cwd.rstrip("/").split("/")[-1] if cwd else "Claude"
    dir_ = transcript.rstrip("/").split("/")[-2] if "/projects/" in transcript else ""

    if event == "Notification":
        title = f"⏳ {proj} needs you"
        body = data.get("message", "Claude is waiting for your input")
    else:  # Stop
        title = f"✅ {proj}"
        body = last_assistant_text(transcript) or "Finished a turn"
    body = body.replace("\n", " ").strip()
    if len(body) > 180:
        body = body[:177] + "…"

    payload = json.dumps({
        "title": title, "event": event, "message": body, "dir": dir_,
        "session_id": session_id, "transcript": transcript,
    }).encode()
    try:
        req = urllib.request.Request(SERVER, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass


if __name__ == "__main__":
    main()
