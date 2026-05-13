# Skill: calendar-google (Peak v1.0)

## Purpose
Manage Google Calendar: view upcoming events, create meetings,
set reminders, and provide daily briefings.

## When This Skill Applies
- "What's on my calendar today?"
- "Schedule a meeting with [person] at [time]"
- "Block 2 hours tomorrow for deep work"
- "Remind me about [thing] at [time]"
- Proactive: daily morning briefing of today's schedule
- Proactive: 15-minute reminder before meetings

## Setup
1. Create OAuth credentials at console.cloud.google.com
2. Store credentials at /data/.hermes/credentials/google_calendar.json
3. First run triggers OAuth flow (one-time)
4. Token stored at /data/.hermes/credentials/google_token.json

## Environment Variables
- GOOGLE_CALENDAR_CREDENTIALS: Path to OAuth credentials JSON

## View today's events
python3 /app/skills/calendar-google/calendar_client.py today

## View upcoming (next 7 days)
python3 /app/skills/calendar-google/calendar_client.py upcoming --days 7

## Create event
python3 /app/skills/calendar-google/calendar_client.py create \
  --title "Meeting with investor" \
  --start "2025-02-15T10:00:00" \
  --end "2025-02-15T11:00:00" \
  --description "Discuss SAFE terms"

## Delete event
python3 /app/skills/calendar-google/calendar_client.py delete --event-id "abc123"

## Daily briefing (cron)
Schedule: 0 7 * * * (7 AM daily)
Output: Today's events formatted as timeline + any conflicts flagged

## Error Handling
- If credentials expired: refresh token automatically
- If refresh fails: alert operator to re-authenticate
- If API quota exceeded: retry with backoff
