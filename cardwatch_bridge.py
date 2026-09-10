#!/usr/bin/env python3
"""
Checks a Gmail inbox (via IMAP) for new CardWatch alert emails and forwards
each one to a Discord webhook. Designed to be run frequently (every 2-3
minutes) on a schedule - since this only reads your own inbox, there's no
"politeness" throttling needed the way there is for the store monitors.

State is tracked using IMAP's own "seen/unseen" flags - each run only looks
at UNSEEN emails matching the sender filter, and marks them as read after
successfully forwarding. This means no separate snapshot/state file is
needed; the mailbox itself is the source of truth.

Requires the following environment variables:
  GMAIL_ADDRESS       - the Gmail address to check (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD  - a Gmail App Password (NOT your normal password -
                        requires 2-Step Verification enabled on the account,
                        generated at https://myaccount.google.com/apppasswords)
  DISCORD_WEBHOOK_URL - the Discord webhook URL to post alerts to
"""

import email
import imaplib
import os
import sys
from email.header import decode_header

import requests

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

# Matches CardWatch's sending domain. If you find their exact "From"
# address, replace this with that full address for a more precise match
# (e.g. "alerts@cardwatch.com.au").
SENDER_FILTER = "cardwatch.com.au"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


def decode_mime_words(s):
    """Decodes an email header (subject, etc.) that may be MIME-encoded."""
    if not s:
        return ""
    decoded_parts = decode_header(s)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def get_plain_text_body(msg):
    """Extracts the plain-text body from an email message, falling back to
    a stripped-down version of the HTML body if no plain-text part exists."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
        # No plain-text part found - fall back to HTML, stripped of tags
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    import re
                    text = re.sub(r"<[^>]+>", " ", html)
                    return re.sub(r"\s+", " ", text).strip()
                except Exception:
                    continue
        return "(Could not extract email body)"
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            return "(Could not extract email body)"


def send_discord(subject, body):
    if not DISCORD_WEBHOOK_URL:
        print(f"[No Discord webhook configured - would have sent] {subject}")
        return

    # Discord embed descriptions cap around 4096 chars - truncate generously
    trimmed_body = body.strip()
    if len(trimmed_body) > 1500:
        trimmed_body = trimmed_body[:1500] + "..."

    payload = {
        "embeds": [
            {
                "title": f"📧 CardWatch Alert: {subject}",
                "description": trimmed_body or "(empty email body)",
                "color": 0x00B0F4,
            }
        ]
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code not in (200, 204):
        print(f"Discord error: {resp.status_code} {resp.text}", file=sys.stderr)


def main():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.", file=sys.stderr)
        sys.exit(1)

    print("Connecting to Gmail...")
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    except imaplib.IMAP4.error as e:
        print(f"Failed to log in: {e}", file=sys.stderr)
        sys.exit(1)

    mail.select("inbox")

    # Search for unread emails from CardWatch specifically
    search_criteria = f'(UNSEEN FROM "{SENDER_FILTER}")'
    status, message_ids = mail.search(None, search_criteria)

    if status != "OK":
        print("IMAP search failed.", file=sys.stderr)
        mail.logout()
        sys.exit(1)

    ids = message_ids[0].split()
    print(f"Found {len(ids)} new CardWatch email(s).")

    for msg_id in ids:
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            print(f"Failed to fetch message {msg_id}", file=sys.stderr)
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_words(msg.get("Subject", "(no subject)"))
        body = get_plain_text_body(msg)

        print(f"Forwarding: {subject}")
        send_discord(subject, body)

        # Mark as read so it isn't processed again next run
        mail.store(msg_id, "+FLAGS", "\\Seen")

    mail.logout()
    print("Done.")


if __name__ == "__main__":
    main()
