#!/usr/bin/env bash
# send-text — send an iMessage from this Mac (as the signed-in Apple ID).
#
# Usage:
#   send_imessage.sh "<recipient>" "<message>"        recipient = +E.164 phone or email
#   send_imessage.sh --chat "<chat-guid>" "<message>" send into an existing thread (most reliable)
#
# Requires Messages.app signed in + Automation permission to control Messages.
# Ground Control's Text Assistant calls this to send a reply. Shipped by install.sh
# into ~/.claude/skills/send-text/ because that is where the server looks for it.
set -euo pipefail

if [[ "${1:-}" == "--chat" ]]; then
  GUID="${2:?chat guid required}"; MSG="${3:?message required}"
  osascript - "$MSG" "$GUID" <<'OSA'
on run {msg, guid}
  tell application "Messages" to send msg to chat id guid
end run
OSA
  echo "sent → chat $GUID"
else
  RECIP="${1:?recipient (phone/email) required}"; MSG="${2:?message required}"
  # A recipient can have multiple threads (iMessage / RCS / SMS). The iMessage
  # one is the reliable AppleScript target — prefer it. Look it up from chat.db,
  # matching the number with/without the +1 country code.
  DIGITS="$(printf '%s' "$RECIP" | tr -cd '0-9')"
  GUID="$(sqlite3 ~/Library/Messages/chat.db "
    SELECT guid FROM chat
    WHERE service_name='iMessage'
      AND replace(replace(replace(replace(chat_identifier,'+',''),'-',''),' ',''),'(','') LIKE '%${DIGITS}%'
    ORDER BY ROWID DESC LIMIT 1;" 2>/dev/null)"
  if [[ -n "$GUID" ]]; then
    osascript - "$MSG" "$GUID" <<'OSA'
on run {msg, guid}
  tell application "Messages" to send msg to chat id guid
end run
OSA
    echo "sent → $RECIP (iMessage thread $GUID)"
  else
    # No existing iMessage thread — start one via the buddy/participant form.
    osascript - "$RECIP" "$MSG" <<'OSA'
on run {recip, msg}
  tell application "Messages"
    set svc to 1st service whose service type = iMessage
    send msg to participant recip of svc
  end tell
end run
OSA
    # AppleScript returning 0 only means Messages.app ACCEPTED the send. Whether it
    # went anywhere shows up in chat.db a few seconds later. A number that is not
    # registered with iMessage fails here with error 22 — silently, unless we look.
    # (2026-08-24: a business text was reported "sent + verified" while error=22.)
    if python3 "$(dirname "$0")/_verify_send.py" "$RECIP" 14; then
      echo "sent → $RECIP (new iMessage thread)"
    else
      echo "iMessage failed — retrying over SMS…" >&2
      osascript - "$RECIP" "$MSG" <<'OSA'
on run {recip, msg}
  tell application "Messages"
    set svc to 1st service whose service type = SMS
    send msg to participant recip of svc
  end tell
end run
OSA
      # SMS from a Mac only works while the iPhone has Text Message Forwarding on,
      # so this second attempt is not guaranteed either — verify it too, and let a
      # real failure exit non-zero rather than printing a comforting "sent".
      if python3 "$(dirname "$0")/_verify_send.py" "$RECIP" 20; then
        echo "sent → $RECIP (SMS — number is not on iMessage)"
      else
        echo "NOT SENT → $RECIP — both iMessage and SMS failed" >&2
        exit 1
      fi
    fi
  fi
fi
