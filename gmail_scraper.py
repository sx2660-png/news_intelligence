"""
Gmail Email Scraper
Fetches all emails from a Gmail inbox via IMAP and extracts
the sent date and body of each message.
Credentials are loaded from environment variables.
"""

import imaplib
import email
import email.message
import os
import json
import html
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime


# ── Configuration (loaded from environment variables) ────────────────
GMAIL_USER     = os.environ.get("GMAIL_USER", "")      # Gmail address
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # App Password (not login password)
IMAP_HOST      = "imap.gmail.com"
IMAP_PORT      = 993

# Output file
OUTPUT_FILE = "emails_output.json"


# ── Utility functions ────────────────────────────────────────────────

def decode_str(value: str | bytes, charset: str = "utf-8") -> str:
    """Safely decode a bytes or str value using the given charset."""
    if isinstance(value, bytes):
        try:
            return value.decode(charset or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            return value.decode("utf-8", errors="replace")
    return value


def decode_mime_header(raw_header: str) -> str:
    """Decode a MIME encoded-word header (e.g. =?utf-8?B?...?= format)."""
    parts = decode_header(raw_header or "")
    result = []
    for part, charset in parts:
        result.append(decode_str(part, charset))
    return "".join(result)


def html_to_text(html_content: str) -> str:
    """Strip HTML tags and return readable plain text."""
    # Remove <style> / <script> blocks
    html_content = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html_content, flags=re.S | re.I)
    # Replace block-level / line-break tags with newlines
    html_content = re.sub(r"<(br|p|div|tr|h[1-6])[^>]*>", "\n", html_content, flags=re.I)
    # Strip remaining tags
    html_content = re.sub(r"<[^>]+>", "", html_content)
    # Unescape HTML entities
    html_content = html.unescape(html_content)
    # Collapse excessive blank lines
    html_content = re.sub(r"\n{3,}", "\n\n", html_content)
    return html_content.strip()


def extract_body(msg: email.message.Message) -> str:
    """
    Extract plain-text body from an email.message.Message.
    Priority: text/plain > text/html (converted to plain text).
    """
    plain_parts = []
    html_parts  = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype    = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = decode_str(payload, charset)
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = decode_str(payload, charset)
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return ""


# ── Core scraping logic ──────────────────────────────────────────────

def connect_gmail() -> imaplib.IMAP4_SSL:
    """Open an IMAP SSL connection to Gmail and log in."""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        raise ValueError(
            "Gmail credentials not found.\n"
            "Please set the following environment variables:\n"
            "  export GMAIL_USER='your@gmail.com'\n"
            "  export GMAIL_PASSWORD='your_app_password'\n\n"
            "Note: Gmail requires 2-Step Verification and an App Password.\n"
            "Generate one at: https://myaccount.google.com/apppasswords"
        )
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    print(f"[✓] Logged in as: {GMAIL_USER}")
    return mail


def fetch_all_emails(mail: imaplib.IMAP4_SSL, mailbox: str = "INBOX") -> list[dict]:
    """
    Fetch all emails from the specified mailbox folder.
    Each item in the returned list contains:
        - uid        unique message ID
        - date       sent date (ISO 8601 string)
        - subject    email subject
        - sender     From address
        - body       plain-text body
    """
    mail.select(mailbox, readonly=True)

    # Search for all messages
    status, data = mail.search(None, "ALL")
    if status != "OK":
        print("[!] Failed to search emails")
        return []

    email_ids = data[0].split()
    total = len(email_ids)
    print(f"[i] Found {total} emails. Starting fetch...")

    results = []
    for idx, eid in enumerate(email_ids, 1):
        try:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Parse sent date
            date_str = msg.get("Date", "")
            try:
                dt = parsedate_to_datetime(date_str)
                date_iso = dt.isoformat()
            except Exception:
                date_iso = date_str

            subject = decode_mime_header(msg.get("Subject", "(no subject)"))
            sender  = decode_mime_header(msg.get("From",    "(unknown sender)"))
            body    = extract_body(msg)

            results.append({
                "uid":     eid.decode(),
                "date":    date_iso,
                "subject": subject,
                "sender":  sender,
                "body":    body,
            })

            if idx % 50 == 0 or idx == total:
                print(f"  Progress: {idx}/{total}")

        except Exception as e:
            print(f"[!] Error processing email {eid}: {e}")
            continue

    return results


def save_results(results: list[dict], path: str = OUTPUT_FILE) -> None:
    """Save results to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[✓] Saved {len(results)} emails to: {path}")


# ── Entry point ──────────────────────────────────────────────────────

def main():
    mail = connect_gmail()
    try:
        emails = fetch_all_emails(mail)
        if emails:
            save_results(emails)
            # Print a preview of the first 3 emails
            print("\n── Preview: first 3 emails ──")
            for e in emails[:3]:
                print(f"  Date:    {e['date']}")
                print(f"  Subject: {e['subject']}")
                print(f"  From:    {e['sender']}")
                preview = e["body"][:200].replace("\n", " ")
                print(f"  Body (first 200 chars): {preview}...")
                print()
    finally:
        mail.logout()
        print("[✓] Logged out")


if __name__ == "__main__":
    main()
