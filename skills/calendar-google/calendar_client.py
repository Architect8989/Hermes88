#!/usr/bin/env python3
"""
Google Calendar Client Skill for Hermes88.
Manages Google Calendar: view events, create meetings, set reminders,
and provide daily briefings via the Google Calendar API.

Uses OAuth 2.0 for authentication with stored credentials.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class CalendarEvent:
    """Represents a calendar event."""
    id: str = ""
    title: str = ""
    description: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    attendees: list = field(default_factory=list)
    status: str = "confirmed"
    is_all_day: bool = False
    recurring: bool = False
    calendar_id: str = "primary"

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "start": self.start,
            "end": self.end,
            "location": self.location,
            "attendees": self.attendees,
            "status": self.status,
            "is_all_day": self.is_all_day,
        }

    def summary(self) -> str:
        """One-line summary for display."""
        time_str = self.start.split("T")[1][:5] if "T" in self.start else "all day"
        return f"{time_str} - {self.title}"


class GoogleOAuth:
    """Handles Google OAuth 2.0 token management."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, credentials_path: str = "",
                 token_path: str = ""):
        """
        Initialize OAuth manager.

        Args:
            credentials_path: Path to OAuth client credentials JSON.
            token_path: Path to stored token JSON.
        """
        self.credentials_path = Path(
            credentials_path or os.environ.get(
                "GOOGLE_CALENDAR_CREDENTIALS",
                "/data/.hermes/credentials/google_calendar.json"
            )
        )
        self.token_path = Path(
            token_path or "/data/.hermes/credentials/google_token.json"
        )
        self._credentials: dict = {}
        self._token: dict = {}

    def get_access_token(self) -> str:
        """
        Get a valid access token, refreshing if needed.

        Returns:
            Valid access token string.
        """
        # Load stored token
        if self.token_path.exists():
            self._token = json.loads(self.token_path.read_text())

            # Check if token is still valid
            expires_at = self._token.get("expires_at", 0)
            if time.time() < expires_at - 60:
                return self._token.get("access_token", "")

            # Try to refresh
            if self._token.get("refresh_token"):
                return self._refresh_token()

        raise RuntimeError(
            "No valid token found. Run initial OAuth flow first.\n"
            "See SKILL.md for setup instructions."
        )

    def _refresh_token(self) -> str:
        """Refresh the access token using the refresh token."""
        self._load_credentials()

        installed = self._credentials.get("installed", self._credentials.get("web", {}))
        client_id = installed.get("client_id", "")
        client_secret = installed.get("client_secret", "")

        if not client_id or not client_secret:
            raise RuntimeError("Invalid credentials file: missing client_id/secret")

        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": self._token["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()

        req = urllib.request.Request(
            self.TOKEN_URL, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                token_data = json.loads(resp.read())

            self._token["access_token"] = token_data["access_token"]
            self._token["expires_at"] = time.time() + token_data.get("expires_in", 3600)

            # Save updated token
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(json.dumps(self._token, indent=2))

            return self._token["access_token"]

        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            raise RuntimeError(f"Token refresh failed ({e.code}): {error_body}")

    def _load_credentials(self):
        """Load OAuth credentials from file."""
        if not self.credentials_path.exists():
            raise RuntimeError(
                f"Credentials file not found: {self.credentials_path}"
            )
        self._credentials = json.loads(self.credentials_path.read_text())


class GoogleCalendarClient:
    """
    Google Calendar API client.
    Provides CRUD operations for calendar events and daily briefings.
    """

    BASE_URL = "https://www.googleapis.com/calendar/v3"

    def __init__(self, calendar_id: str = "primary"):
        """
        Initialize calendar client.

        Args:
            calendar_id: Calendar ID to use (default: primary).
        """
        self.calendar_id = calendar_id
        self.oauth = GoogleOAuth()
        self._timezone = os.environ.get("TZ", "UTC")

    def get_today_events(self) -> list:
        """Get all events for today."""
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        return self._list_events(
            time_min=start_of_day.isoformat(),
            time_max=end_of_day.isoformat(),
        )

    def get_upcoming(self, days: int = 7) -> list:
        """Get upcoming events for the next N days."""
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)

        return self._list_events(
            time_min=now.isoformat(),
            time_max=end.isoformat(),
        )

    def create_event(self, title: str, start: str, end: str,
                     description: str = "", location: str = "",
                     attendees: Optional[list] = None) -> Optional[CalendarEvent]:
        """
        Create a new calendar event.

        Args:
            title: Event title/summary.
            start: Start time (ISO 8601 format).
            end: End time (ISO 8601 format).
            description: Event description.
            location: Event location.
            attendees: List of attendee email addresses.

        Returns:
            Created CalendarEvent or None on failure.
        """
        event_body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": self._format_time(start),
            "end": self._format_time(end),
        }

        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees]

        url = f"{self.BASE_URL}/calendars/{self.calendar_id}/events"
        result = self._api_request("POST", url, body=event_body)

        if result:
            event = self._parse_event(result)
            print(f"[calendar] Created: {event.summary()}", flush=True)
            return event
        return None

    def update_event(self, event_id: str, **kwargs) -> Optional[CalendarEvent]:
        """
        Update an existing event.

        Args:
            event_id: ID of the event to update.
            **kwargs: Fields to update (title, start, end, description, location).

        Returns:
            Updated CalendarEvent or None on failure.
        """
        # First, get the existing event
        url = f"{self.BASE_URL}/calendars/{self.calendar_id}/events/{event_id}"
        existing = self._api_request("GET", url)
        if not existing:
            return None

        # Apply updates
        if "title" in kwargs:
            existing["summary"] = kwargs["title"]
        if "description" in kwargs:
            existing["description"] = kwargs["description"]
        if "location" in kwargs:
            existing["location"] = kwargs["location"]
        if "start" in kwargs:
            existing["start"] = self._format_time(kwargs["start"])
        if "end" in kwargs:
            existing["end"] = self._format_time(kwargs["end"])

        result = self._api_request("PUT", url, body=existing)
        if result:
            return self._parse_event(result)
        return None

    def delete_event(self, event_id: str) -> bool:
        """
        Delete a calendar event.

        Args:
            event_id: ID of the event to delete.

        Returns:
            True on success, False on failure.
        """
        url = f"{self.BASE_URL}/calendars/{self.calendar_id}/events/{event_id}"
        try:
            self._api_request("DELETE", url)
            print(f"[calendar] Deleted event: {event_id}", flush=True)
            return True
        except Exception:
            return False

    def daily_briefing(self) -> str:
        """
        Generate a daily briefing of today's schedule.

        Returns:
            Formatted briefing text.
        """
        events = self.get_today_events()

        if not events:
            return "No events scheduled for today. Clear calendar for deep work."

        lines = [f"Today's schedule ({len(events)} events):"]
        for event in events:
            lines.append(f"  {event.summary()}")
            if event.location:
                lines.append(f"    Location: {event.location}")

        # Check for conflicts
        conflicts = self._detect_conflicts(events)
        if conflicts:
            lines.append(f"\nConflicts detected ({len(conflicts)}):")
            for c1, c2 in conflicts:
                lines.append(f"  {c1.title} overlaps with {c2.title}")

        return "\n".join(lines)

    def _list_events(self, time_min: str, time_max: str) -> list:
        """List events within a time range."""
        params = urllib.parse.urlencode({
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
        })
        url = f"{self.BASE_URL}/calendars/{self.calendar_id}/events?{params}"
        result = self._api_request("GET", url)

        if not result:
            return []

        events = []
        for item in result.get("items", []):
            events.append(self._parse_event(item))

        return events

    def _parse_event(self, item: dict) -> CalendarEvent:
        """Parse an API response item into CalendarEvent."""
        start_data = item.get("start", {})
        end_data = item.get("end", {})

        is_all_day = "date" in start_data
        start = start_data.get("dateTime", start_data.get("date", ""))
        end = end_data.get("dateTime", end_data.get("date", ""))

        attendees = [
            a.get("email", "")
            for a in item.get("attendees", [])
        ]

        return CalendarEvent(
            id=item.get("id", ""),
            title=item.get("summary", "(No title)"),
            description=item.get("description", ""),
            start=start,
            end=end,
            location=item.get("location", ""),
            attendees=attendees,
            status=item.get("status", "confirmed"),
            is_all_day=is_all_day,
            recurring="recurringEventId" in item,
        )

    def _format_time(self, time_str: str) -> dict:
        """Format time string for API request."""
        if "T" not in time_str:
            # All-day event
            return {"date": time_str}
        # Ensure timezone
        if not time_str.endswith("Z") and "+" not in time_str and "-" not in time_str[-6:]:
            time_str += "Z"
        return {"dateTime": time_str, "timeZone": self._timezone}

    def _detect_conflicts(self, events: list) -> list:
        """Detect overlapping events."""
        conflicts = []
        for i, e1 in enumerate(events):
            if e1.is_all_day:
                continue
            for e2 in events[i+1:]:
                if e2.is_all_day:
                    continue
                # Simple overlap check
                if e1.end > e2.start and e1.start < e2.end:
                    conflicts.append((e1, e2))
        return conflicts

    def _api_request(self, method: str, url: str,
                     body: Optional[dict] = None) -> Optional[dict]:
        """Make an authenticated API request."""
        try:
            token = self.oauth.get_access_token()
        except RuntimeError as e:
            print(f"[calendar] Auth error: {e}", flush=True)
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 204:
                        return {}
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    # Token expired, force refresh
                    try:
                        token = self.oauth._refresh_token()
                        headers["Authorization"] = f"Bearer {token}"
                        req = urllib.request.Request(
                            url, data=data, headers=headers, method=method
                        )
                        continue
                    except RuntimeError:
                        pass
                elif e.code == 429 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[calendar] API error {e.code}: {e.read().decode()[:200]}", flush=True)
                return None
            except Exception as e:
                print(f"[calendar] Request error: {e}", flush=True)
                return None

        return None


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for calendar client."""
    parser = argparse.ArgumentParser(
        description="Google Calendar Client -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # Today
    sub.add_parser("today", help="Show today's events")

    # Upcoming
    upcoming_p = sub.add_parser("upcoming", help="Show upcoming events")
    upcoming_p.add_argument("--days", type=int, default=7)

    # Create
    create_p = sub.add_parser("create", help="Create an event")
    create_p.add_argument("--title", required=True)
    create_p.add_argument("--start", required=True, help="ISO 8601 start time")
    create_p.add_argument("--end", required=True, help="ISO 8601 end time")
    create_p.add_argument("--description", default="")
    create_p.add_argument("--location", default="")
    create_p.add_argument("--attendees", nargs="*", default=[])

    # Delete
    delete_p = sub.add_parser("delete", help="Delete an event")
    delete_p.add_argument("--event-id", required=True)

    # Briefing
    sub.add_parser("briefing", help="Generate daily briefing")

    args = parser.parse_args()
    client = GoogleCalendarClient()

    if args.command == "today":
        events = client.get_today_events()
        if events:
            for e in events:
                print(e.summary())
        else:
            print("No events today.")

    elif args.command == "upcoming":
        events = client.get_upcoming(days=args.days)
        if events:
            current_date = ""
            for e in events:
                date = e.start.split("T")[0] if "T" in e.start else e.start
                if date != current_date:
                    current_date = date
                    print(f"\n{date}:")
                print(f"  {e.summary()}")
        else:
            print(f"No events in the next {args.days} days.")

    elif args.command == "create":
        event = client.create_event(
            title=args.title,
            start=args.start,
            end=args.end,
            description=args.description,
            location=args.location,
            attendees=args.attendees,
        )
        if event:
            print(f"Created: {event.title} ({event.id})")
        else:
            print("Failed to create event.")
            sys.exit(1)

    elif args.command == "delete":
        success = client.delete_event(args.event_id)
        if not success:
            sys.exit(1)

    elif args.command == "briefing":
        print(client.daily_briefing())

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
