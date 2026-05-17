#!/usr/bin/env python3
"""
Email Client Skill for Hermes88.
Provides IMAP read, SMTP send, search, and VIP monitoring capabilities.
Handles MIME multipart messages, attachments, and HTML/plain text bodies.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import email
import email.mime.multipart
import email.mime.text
import email.mime.base
import email.utils
import imaplib
import json
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Optional


@dataclass
class EmailMessage:
    """Represents a parsed email message."""
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    recipients: list = field(default_factory=list)
    date: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    folder: str = "INBOX"
    uid: str = ""
    is_read: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "sender": self.sender,
            "sender_name": self.sender_name,
            "recipients": self.recipients,
            "date": self.date,
            "body_text": self.body_text[:2000],
            "attachments": self.attachments,
            "is_read": self.is_read,
        }

    def summary(self) -> str:
        """One-line summary for display."""
        read_marker = "" if self.is_read else "[NEW] "
        return f"{read_marker}From: {self.sender_name or self.sender} | {self.subject} | {self.date}"


class IMAPClient:
    """IMAP client for reading and searching emails."""

    def __init__(self, host: str = "", port: int = 993,
                 username: str = "", password: str = ""):
        """
        Initialize IMAP client.

        Args:
            host: IMAP server hostname.
            port: IMAP port (default: 993 for SSL).
            username: Login username/email.
            password: Login password/app password.
        """
        self.host = host or os.environ.get("IMAP_HOST", "imap.gmail.com")
        self.port = port or int(os.environ.get("IMAP_PORT", "993"))
        self.username = username or os.environ.get("IMAP_USER", "")
        self.password = password or os.environ.get("IMAP_PASSWORD", "")
        self._connection: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        """Establish IMAP connection with SSL."""
        if not self.username or not self.password:
            raise ValueError("IMAP credentials not configured")

        context = ssl.create_default_context()
        self._connection = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=context
        )
        self._connection.login(self.username, self.password)
        print(f"[email] Connected to {self.host} as {self.username}", flush=True)

    def disconnect(self):
        """Close IMAP connection."""
        if self._connection:
            try:
                self._connection.logout()
            except Exception:
                pass
            self._connection = None

    def list_folders(self) -> list:
        """List available mail folders."""
        if not self._connection:
            self.connect()
        status, folders = self._connection.list()
        result = []
        for folder in folders:
            if isinstance(folder, bytes):
                decoded = folder.decode(errors="replace")
                # Extract folder name from IMAP response
                parts = decoded.split('" "')
                if len(parts) >= 2:
                    result.append(parts[-1].strip('"'))
                else:
                    result.append(decoded)
        return result

    def read_messages(self, folder: str = "INBOX", unread_only: bool = False,
                      limit: int = 10, since_days: int = 7) -> list:
        """
        Read messages from a folder.

        Args:
            folder: Mail folder name.
            unread_only: Only fetch unread messages.
            limit: Maximum messages to return.
            since_days: Only fetch messages from last N days.

        Returns:
            List of EmailMessage objects.
        """
        if not self._connection:
            self.connect()

        self._connection.select(folder, readonly=True)

        # Build search criteria
        criteria = []
        if unread_only:
            criteria.append("UNSEEN")
        if since_days > 0:
            since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            criteria.append(f'SINCE "{since_date}"')

        search_str = " ".join(criteria) if criteria else "ALL"
        status, data = self._connection.search(None, search_str)

        if status != "OK":
            return []

        message_ids = data[0].split()
        # Take the most recent messages
        message_ids = message_ids[-limit:]

        messages = []
        for mid in reversed(message_ids):
            msg = self._fetch_message(mid, folder)
            if msg:
                messages.append(msg)

        return messages

    def search(self, query: str, folder: str = "INBOX",
               limit: int = 10) -> list:
        """
        Search for messages matching a query.

        Supports simplified query syntax:
        - from:address -> FROM "address"
        - subject:text -> SUBJECT "text"
        - to:address -> TO "address"
        - body:text -> BODY "text"
        - Plain text -> OR SUBJECT/FROM/BODY

        Args:
            query: Search query string.
            folder: Folder to search.
            limit: Maximum results.

        Returns:
            List of EmailMessage objects.
        """
        if not self._connection:
            self.connect()

        self._connection.select(folder, readonly=True)

        # Parse query into IMAP search criteria
        imap_criteria = self._parse_query(query)
        status, data = self._connection.search(None, imap_criteria)

        if status != "OK":
            return []

        message_ids = data[0].split()[-limit:]
        messages = []
        for mid in reversed(message_ids):
            msg = self._fetch_message(mid, folder)
            if msg:
                messages.append(msg)

        return messages

    def mark_read(self, uid: str, folder: str = "INBOX"):
        """Mark a message as read."""
        if not self._connection:
            self.connect()
        self._connection.select(folder)
        self._connection.store(uid.encode(), "+FLAGS", "\\Seen")

    def _fetch_message(self, message_id: bytes, folder: str) -> Optional[EmailMessage]:
        """Fetch and parse a single message."""
        try:
            status, data = self._connection.fetch(message_id, "(RFC822 FLAGS)")
            if status != "OK" or not data or not data[0]:
                return None

            raw_email = data[0][1] if isinstance(data[0], tuple) else data[0]
            if isinstance(raw_email, bytes):
                msg = email.message_from_bytes(raw_email)
            else:
                return None

            # Parse flags
            flags = []
            if len(data) > 1 and data[1]:
                flags_str = data[1].decode(errors="replace") if isinstance(data[1], bytes) else str(data[1])
                if "\\Seen" in flags_str:
                    flags.append("seen")

            return self._parse_message(msg, folder, message_id.decode(), flags)

        except Exception as e:
            print(f"[email] Parse error: {e}", flush=True)
            return None

    def _parse_message(self, msg: email.message.Message, folder: str,
                       uid: str, flags: list) -> EmailMessage:
        """Parse an email.message.Message into EmailMessage."""
        # Decode subject
        subject = self._decode_header(msg.get("Subject", ""))

        # Parse sender
        from_header = msg.get("From", "")
        sender_name, sender_addr = email.utils.parseaddr(from_header)
        sender_name = self._decode_header(sender_name)

        # Parse recipients
        to_header = msg.get("To", "")
        recipients = [addr for _, addr in email.utils.getaddresses([to_header])]

        # Parse date
        date_str = msg.get("Date", "")
        try:
            date_parsed = email.utils.parsedate_to_datetime(date_str)
            date_formatted = date_parsed.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_formatted = date_str[:20]

        # Extract body
        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in disposition:
                    filename = part.get_filename() or "unnamed"
                    attachments.append(self._decode_header(filename))
                elif content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode(errors="replace")
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_html = payload.decode(errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                if msg.get_content_type() == "text/html":
                    body_html = payload.decode(errors="replace")
                else:
                    body_text = payload.decode(errors="replace")

        return EmailMessage(
            message_id=msg.get("Message-ID", ""),
            subject=subject,
            sender=sender_addr,
            sender_name=sender_name,
            recipients=recipients,
            date=date_formatted,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            flags=flags,
            folder=folder,
            uid=uid,
            is_read="seen" in flags,
        )

    def _decode_header(self, header: str) -> str:
        """Decode MIME-encoded header."""
        if not header:
            return ""
        decoded_parts = decode_header(header)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result)

    def _parse_query(self, query: str) -> str:
        """Parse simplified query syntax into IMAP search criteria."""
        parts = query.split()
        criteria = []

        for part in parts:
            if part.startswith("from:"):
                criteria.append(f'FROM "{part[5:]}"')
            elif part.startswith("subject:"):
                criteria.append(f'SUBJECT "{part[8:]}"')
            elif part.startswith("to:"):
                criteria.append(f'TO "{part[3:]}"')
            elif part.startswith("body:"):
                criteria.append(f'BODY "{part[5:]}"')
            else:
                # Default: search subject
                criteria.append(f'SUBJECT "{part}"')

        return " ".join(criteria) if criteria else "ALL"


class SMTPClient:
    """SMTP client for sending emails."""

    def __init__(self, host: str = "", port: int = 587,
                 username: str = "", password: str = ""):
        """
        Initialize SMTP client.

        Args:
            host: SMTP server hostname.
            port: SMTP port (default: 587 for STARTTLS).
            username: Login username/email.
            password: Login password/app password.
        """
        self.host = host or os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.port = port or int(os.environ.get("SMTP_PORT", "587"))
        self.username = username or os.environ.get("SMTP_USER", "")
        self.password = password or os.environ.get("SMTP_PASSWORD", "")

    def send(self, to: str, subject: str, body: str,
             cc: str = "", bcc: str = "",
             html: bool = False, reply_to: str = "",
             attachments: Optional[list] = None) -> bool:
        """
        Send an email.

        Args:
            to: Recipient email (comma-separated for multiple).
            subject: Email subject.
            body: Email body text.
            cc: CC recipients.
            bcc: BCC recipients.
            html: Whether body is HTML.
            reply_to: In-Reply-To header (for threading).
            attachments: List of file paths to attach.

        Returns:
            True on success, False on failure.
        """
        if not self.username or not self.password:
            raise ValueError("SMTP credentials not configured")

        # Build message
        if attachments:
            msg = email.mime.multipart.MIMEMultipart()
            if html:
                msg.attach(email.mime.text.MIMEText(body, "html"))
            else:
                msg.attach(email.mime.text.MIMEText(body, "plain"))

            for filepath in attachments:
                self._attach_file(msg, filepath)
        else:
            subtype = "html" if html else "plain"
            msg = email.mime.text.MIMEText(body, subtype)

        msg["From"] = self.username
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.host)

        if cc:
            msg["Cc"] = cc
        if reply_to:
            msg["In-Reply-To"] = reply_to
            msg["References"] = reply_to

        # Collect all recipients
        all_recipients = [addr.strip() for addr in to.split(",")]
        if cc:
            all_recipients.extend(addr.strip() for addr in cc.split(","))
        if bcc:
            all_recipients.extend(addr.strip() for addr in bcc.split(","))

        # Send
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.host, self.port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(self.username, self.password)
                server.sendmail(self.username, all_recipients, msg.as_string())

            print(f"[email] Sent to: {to} | Subject: {subject}", flush=True)
            return True

        except smtplib.SMTPAuthenticationError as e:
            print(f"[email] Auth failed: {e}", flush=True)
            return False
        except smtplib.SMTPException as e:
            print(f"[email] SMTP error: {e}", flush=True)
            return False
        except Exception as e:
            print(f"[email] Send error: {e}", flush=True)
            return False

    def _attach_file(self, msg: email.mime.multipart.MIMEMultipart,
                     filepath: str):
        """Attach a file to the message."""
        path = Path(filepath)
        if not path.exists():
            print(f"[email] Attachment not found: {filepath}", flush=True)
            return

        with open(path, "rb") as f:
            part = email.mime.base.MIMEBase("application", "octet-stream")
            part.set_payload(f.read())

        email.encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename={path.name}",
        )
        msg.attach(part)


class VIPMonitor:
    """Monitors for emails from VIP senders."""

    def __init__(self, vip_file: str = "/data/.hermes/config/vip_contacts.json"):
        """
        Initialize VIP monitor.

        Args:
            vip_file: Path to VIP contacts JSON file.
        """
        self.vip_file = Path(vip_file)
        self._vip_list: list = []
        self._load_vip_list()

    def _load_vip_list(self):
        """Load VIP contacts from file."""
        if self.vip_file.exists():
            try:
                data = json.loads(self.vip_file.read_text())
                self._vip_list = data.get("vip_senders", [])
            except Exception:
                self._vip_list = []

    def is_vip(self, sender: str) -> bool:
        """Check if a sender is on the VIP list."""
        sender_lower = sender.lower()
        return any(
            vip.lower() in sender_lower
            for vip in self._vip_list
        )

    def check_vip_emails(self, messages: list) -> list:
        """Filter messages to only VIP senders."""
        return [msg for msg in messages if self.is_vip(msg.sender)]


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for email client."""
    parser = argparse.ArgumentParser(
        description="Email Client -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # Read
    read_p = sub.add_parser("read", help="Read emails")
    read_p.add_argument("--folder", default="INBOX")
    read_p.add_argument("--unread-only", action="store_true")
    read_p.add_argument("--limit", type=int, default=10)
    read_p.add_argument("--since-days", type=int, default=7)
    read_p.add_argument("--json-output", action="store_true")

    # Send
    send_p = sub.add_parser("send", help="Send email")
    send_p.add_argument("--to", required=True)
    send_p.add_argument("--subject", required=True)
    send_p.add_argument("--body", required=True)
    send_p.add_argument("--cc", default="")
    send_p.add_argument("--html", action="store_true")
    send_p.add_argument("--attach", nargs="*", default=[])

    # Reply
    reply_p = sub.add_parser("reply", help="Reply to email")
    reply_p.add_argument("--message-id", required=True)
    reply_p.add_argument("--body", required=True)

    # Search
    search_p = sub.add_parser("search", help="Search emails")
    search_p.add_argument("--query", required=True)
    search_p.add_argument("--folder", default="INBOX")
    search_p.add_argument("--limit", type=int, default=10)

    # VIP check
    vip_p = sub.add_parser("vip-check", help="Check for VIP emails")
    vip_p.add_argument("--folder", default="INBOX")

    args = parser.parse_args()

    if args.command == "read":
        client = IMAPClient()
        try:
            client.connect()
            messages = client.read_messages(
                folder=args.folder,
                unread_only=args.unread_only,
                limit=args.limit,
                since_days=args.since_days,
            )
            if args.json_output:
                print(json.dumps([m.to_dict() for m in messages], indent=2))
            else:
                for msg in messages:
                    print(msg.summary())
                    if msg.body_text:
                        print(f"  {msg.body_text[:200]}")
                    print()
            print(f"[email] {len(messages)} messages retrieved", flush=True)
        finally:
            client.disconnect()

    elif args.command == "send":
        client = SMTPClient()
        success = client.send(
            to=args.to,
            subject=args.subject,
            body=args.body,
            cc=args.cc,
            html=args.html,
            attachments=args.attach,
        )
        sys.exit(0 if success else 1)

    elif args.command == "reply":
        smtp = SMTPClient()
        success = smtp.send(
            to="",  # Will be filled from original message
            subject="Re: ",
            body=args.body,
            reply_to=args.message_id,
        )
        sys.exit(0 if success else 1)

    elif args.command == "search":
        client = IMAPClient()
        try:
            client.connect()
            messages = client.search(
                query=args.query,
                folder=args.folder,
                limit=args.limit,
            )
            for msg in messages:
                print(msg.summary())
            print(f"\n[email] {len(messages)} results", flush=True)
        finally:
            client.disconnect()

    elif args.command == "vip-check":
        client = IMAPClient()
        monitor = VIPMonitor()
        try:
            client.connect()
            messages = client.read_messages(
                folder=args.folder, unread_only=True, limit=20,
            )
            vip_messages = monitor.check_vip_emails(messages)
            if vip_messages:
                print(f"[email] {len(vip_messages)} VIP emails found:")
                for msg in vip_messages:
                    print(f"  {msg.summary()}")
            else:
                print("[email] No new VIP emails", flush=True)
        finally:
            client.disconnect()

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
