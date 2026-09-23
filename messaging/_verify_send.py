#!/usr/bin/env python3
"""Did that message ACTUALLY send? Exit 0 only if the carrier/Apple accepted it.

Why this exists: AppleScript returns success the moment Messages.app *accepts* the
send. Whether it then went anywhere is a separate fact, recorded seconds later in
chat.db as `is_sent` / `error`. On 2026-08-24 a text to a business landed in the
thread, read back fine, and was reported to the user as "sent + verified" — while the
row said `is_sent=0, error=22`, because the number is not on iMessage and the
blue-bubble send bounced. Phil found out by looking at his phone.

So: reading the thread back is NOT verification. This is.

Usage:  _verify_send.py "<number-or-chat-identifier>" [timeout_seconds]
Prints one status line; exit 0 = sent, 1 = failed, 2 = still pending.
"""
import os
import sqlite3
import sys
import time

DB = os.path.expanduser("~/Library/Messages/chat.db")

# Messages error codes seen in the wild. Anything non-zero is a failure; these just
# make the report say something useful instead of a bare number.
ERRORS = {
    22: "not delivered — that number is not on iMessage (retry over SMS)",
    3:  "send failed (network or service unavailable)",
    1:  "generic send failure",
}


def latest(ident: str):
    digits = "".join(ch for ch in ident if ch.isdigit())
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute("""
            SELECT m.ROWID, m.is_sent, m.is_delivered, m.error, ch.service_name,
                   datetime(m.date/1000000000 + 978307200,'unixepoch','localtime')
            FROM message m
            JOIN chat_message_join j ON j.message_id = m.ROWID
            JOIN chat ch ON ch.ROWID = j.chat_id
            WHERE m.is_from_me = 1
              AND replace(replace(replace(replace(ch.chat_identifier,'+',''),'-',''),' ',''),'(','')
                  LIKE ?
            ORDER BY m.date DESC LIMIT 1
        """, (f"%{digits}%",)).fetchone()
        return row
    finally:
        con.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _verify_send.py <number> [timeout]", file=sys.stderr)
        return 2
    ident = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = latest(ident)
        if row:
            _, is_sent, delivered, err, svc, when = row
            if err:
                print(f"FAILED  {when}  service={svc}  error={err} — "
                      f"{ERRORS.get(err, 'unknown error')}")
                return 1
            if is_sent:
                d = "delivered" if delivered else "sent (no delivery receipt yet)"
                print(f"SENT    {when}  service={svc}  {d}")
                return 0
        time.sleep(2)
    if row:
        print(f"PENDING still unsent after {timeout:.0f}s — treat as NOT sent")
    else:
        print("PENDING no outbound row found — treat as NOT sent")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
