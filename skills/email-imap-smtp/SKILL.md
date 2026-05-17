# Skill: email-imap-smtp (Peak v1.0)

## Purpose
Read, send, and manage email on behalf of the operator.
Supports IMAP (read) and SMTP (send) protocols.

## When This Skill Applies
- "Check my email"
- "Send an email to [recipient] about [topic]"
- "Reply to the latest email from [sender]"
- "Summarize unread emails"
- Proactive: new email from VIP sender triggers alert

## Environment Variables
- IMAP_HOST: IMAP server hostname (e.g., imap.gmail.com)
- IMAP_PORT: IMAP port (default: 993)
- IMAP_USER: Email address for IMAP login
- IMAP_PASSWORD: App password for IMAP
- SMTP_HOST: SMTP server hostname (e.g., smtp.gmail.com)
- SMTP_PORT: SMTP port (default: 587)
- SMTP_USER: Email address for SMTP login
- SMTP_PASSWORD: App password for SMTP

## Read emails
python3 /app/skills/email-imap-smtp/email_client.py read \
  --folder INBOX \
  --unread-only \
  --limit 10

## Send email
python3 /app/skills/email-imap-smtp/email_client.py send \
  --to "recipient@example.com" \
  --subject "Subject line" \
  --body "Email body text"

## Reply to email
python3 /app/skills/email-imap-smtp/email_client.py reply \
  --message-id "<msg-id@mail.gmail.com>" \
  --body "Reply text"

## Search
python3 /app/skills/email-imap-smtp/email_client.py search \
  --query "from:investor subject:term sheet"

## Protocol
1. Connect to IMAP server with TLS
2. Select folder (INBOX, Sent, etc.)
3. Fetch messages matching criteria
4. Parse headers + body (handle MIME multipart)
5. Return structured summary to Hermes
6. For sends: compose MIME message, connect SMTP, send

## Proactive Monitoring
Every 5 minutes, check for unread emails from VIP senders.
VIP list stored in /data/.hermes/config/vip_contacts.json
Alert operator via Telegram if VIP email arrives.
