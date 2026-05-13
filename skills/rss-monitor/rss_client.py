#!/usr/bin/env python3
"""
RSS/Atom Feed Monitor Skill for Hermes88.
Parses RSS 2.0, Atom, and JSON Feed formats. Provides keyword filtering,
deduplication via SQLite, importance scoring, and synthesis of new items.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET


@dataclass
class FeedConfig:
    """Configuration for a single RSS feed."""
    url: str = ""
    category: str = "general"
    keywords: list = field(default_factory=list)
    importance: str = "medium"  # high, medium, low


@dataclass
class FeedItem:
    """Represents a single feed item/entry."""
    id: str = ""
    title: str = ""
    url: str = ""
    description: str = ""
    published: str = ""
    source_url: str = ""
    source_category: str = ""
    keyword_matches: list = field(default_factory=list)
    score: float = 0.0

    def summary(self) -> str:
        """One-line summary."""
        keywords = ", ".join(self.keyword_matches[:3]) if self.keyword_matches else ""
        kw_str = f" [{keywords}]" if keywords else ""
        return f"[{self.source_category}]{kw_str} {self.title}"


class FeedParser:
    """
    Parses RSS 2.0, Atom, and JSON Feed formats.
    Handles different XML namespaces and date formats.
    """

    # Common Atom namespace
    ATOM_NS = "http://www.w3.org/2005/Atom"

    def parse(self, content: str, source_url: str = "") -> list:
        """
        Parse feed content into FeedItem list.

        Args:
            content: Raw feed content (XML or JSON).
            source_url: URL of the feed source.

        Returns:
            List of FeedItem objects.
        """
        content = content.strip()

        # Try JSON Feed first
        if content.startswith("{"):
            return self._parse_json_feed(content, source_url)

        # Try XML (RSS/Atom)
        try:
            return self._parse_xml_feed(content, source_url)
        except ET.ParseError as e:
            print(f"[rss] XML parse error for {source_url}: {e}", flush=True)
            return []

    def _parse_xml_feed(self, content: str, source_url: str) -> list:
        """Parse RSS 2.0 or Atom feed from XML."""
        root = ET.fromstring(content)
        tag = root.tag.lower()

        # Remove namespace prefix for tag comparison
        if "}" in tag:
            tag = tag.split("}", 1)[1]

        if tag == "rss":
            return self._parse_rss(root, source_url)
        elif tag == "feed":
            return self._parse_atom(root, source_url)
        elif tag == "rdf":
            return self._parse_rss(root, source_url)  # RSS 1.0
        else:
            # Try finding channel/entry elements
            channel = root.find(".//channel")
            if channel is not None:
                return self._parse_rss(root, source_url)
            return self._parse_atom(root, source_url)

    def _parse_rss(self, root: ET.Element, source_url: str) -> list:
        """Parse RSS 2.0 format."""
        items = []
        for item_el in root.iter("item"):
            title = self._get_text(item_el, "title")
            link = self._get_text(item_el, "link")
            description = self._get_text(item_el, "description")
            pub_date = self._get_text(item_el, "pubDate")
            guid = self._get_text(item_el, "guid") or link

            # Clean HTML from description
            description = self._strip_html(description)[:500]

            item_id = hashlib.sha256(
                (guid or title or link).encode()
            ).hexdigest()[:16]

            items.append(FeedItem(
                id=item_id,
                title=title,
                url=link,
                description=description,
                published=pub_date,
                source_url=source_url,
            ))

        return items

    def _parse_atom(self, root: ET.Element, source_url: str) -> list:
        """Parse Atom feed format."""
        items = []
        ns = {"atom": self.ATOM_NS}

        # Try with and without namespace
        entries = root.findall("atom:entry", ns)
        if not entries:
            entries = root.findall("entry")
        if not entries:
            entries = root.findall(f"{{{self.ATOM_NS}}}entry")

        for entry in entries:
            title = self._get_text_ns(entry, "title", ns)

            # Get link (prefer alternate)
            link = ""
            for link_el in entry.findall("atom:link", ns) or entry.findall("link"):
                rel = link_el.get("rel", "alternate")
                if rel == "alternate":
                    link = link_el.get("href", "")
                    break
            if not link:
                link_el = entry.find("link") or entry.find(f"{{{self.ATOM_NS}}}link")
                if link_el is not None:
                    link = link_el.get("href", link_el.text or "")

            summary = self._get_text_ns(entry, "summary", ns)
            content = self._get_text_ns(entry, "content", ns)
            description = self._strip_html(summary or content)[:500]

            published = (
                self._get_text_ns(entry, "published", ns) or
                self._get_text_ns(entry, "updated", ns)
            )

            entry_id = self._get_text_ns(entry, "id", ns) or link or title
            item_id = hashlib.sha256(entry_id.encode()).hexdigest()[:16]

            items.append(FeedItem(
                id=item_id,
                title=title,
                url=link,
                description=description,
                published=published,
                source_url=source_url,
            ))

        return items

    def _parse_json_feed(self, content: str, source_url: str) -> list:
        """Parse JSON Feed format."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []

        items = []
        for item in data.get("items", []):
            title = item.get("title", "")
            url = item.get("url", item.get("external_url", ""))
            description = item.get("summary", item.get("content_text", ""))[:500]
            published = item.get("date_published", "")
            item_id = hashlib.sha256(
                (item.get("id", "") or url or title).encode()
            ).hexdigest()[:16]

            items.append(FeedItem(
                id=item_id,
                title=title,
                url=url,
                description=description,
                published=published,
                source_url=source_url,
            ))

        return items

    def _get_text(self, element: ET.Element, tag: str) -> str:
        """Get text content of a child element."""
        child = element.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    def _get_text_ns(self, element: ET.Element, tag: str, ns: dict) -> str:
        """Get text with namespace fallback."""
        # Try with namespace
        child = element.find(f"atom:{tag}", ns)
        if child is None:
            child = element.find(tag)
        if child is None:
            child = element.find(f"{{{self.ATOM_NS}}}{tag}")
        if child is not None and child.text:
            return child.text.strip()
        return ""

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags from text."""
        if not text:
            return ""
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean)
        return clean.strip()


class DeduplicationStore:
    """SQLite-based store for tracking seen feed items."""

    def __init__(self, db_path: str = "/data/.hermes/rss_seen.db"):
        """Initialize deduplication store."""
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        """Create tables if not exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                feed_url TEXT NOT NULL,
                title TEXT,
                seen_at REAL NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_seen_feed
            ON seen_items(feed_url)
        """)
        self.conn.commit()

    def is_seen(self, item_id: str) -> bool:
        """Check if an item has been seen before."""
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, item_id: str, feed_url: str, title: str = ""):
        """Mark an item as seen."""
        self.conn.execute(
            "INSERT OR REPLACE INTO seen_items (item_id, feed_url, title, seen_at) "
            "VALUES (?, ?, ?, ?)",
            (item_id, feed_url, title, time.time()),
        )
        self.conn.commit()

    def cleanup(self, max_age_days: int = 30):
        """Remove old entries."""
        cutoff = time.time() - (max_age_days * 86400)
        self.conn.execute(
            "DELETE FROM seen_items WHERE seen_at < ?", (cutoff,)
        )
        self.conn.commit()

    def close(self):
        """Close database connection."""
        self.conn.close()


class RSSMonitor:
    """
    Main RSS monitoring orchestrator.
    Fetches feeds, filters by keywords, deduplicates, scores, and
    synthesizes summaries of new items.
    """

    IMPORTANCE_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}

    def __init__(self, config_path: str = "/data/.hermes/config/rss_feeds.json",
                 db_path: str = "/data/.hermes/rss_seen.db"):
        """
        Initialize RSS monitor.

        Args:
            config_path: Path to feed configuration JSON.
            db_path: Path to deduplication database.
        """
        self.config_path = Path(config_path)
        self.feeds: list = []
        self.parser = FeedParser()
        self.dedup = DeduplicationStore(db_path)
        self._load_config()

    def _load_config(self):
        """Load feed configuration from JSON file."""
        if not self.config_path.exists():
            # Create default config
            default_config = {
                "feeds": [
                    {
                        "url": "https://github.blog/feed/",
                        "category": "github",
                        "keywords": ["security", "actions", "copilot"],
                        "importance": "high",
                    },
                    {
                        "url": "https://blog.cloudflare.com/rss/",
                        "category": "security",
                        "keywords": ["zero-day", "DDoS", "WAF"],
                        "importance": "medium",
                    },
                ]
            }
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(default_config, indent=2))

        try:
            data = json.loads(self.config_path.read_text())
            self.feeds = [
                FeedConfig(**f) for f in data.get("feeds", [])
            ]
        except Exception as e:
            print(f"[rss] Config load error: {e}", flush=True)

    def check_all(self) -> list:
        """
        Check all configured feeds for new items.

        Returns:
            List of new FeedItem objects, scored and sorted.
        """
        all_new_items = []

        for feed_config in self.feeds:
            items = self._check_feed(feed_config)
            all_new_items.extend(items)

        # Sort by score (highest first)
        all_new_items.sort(key=lambda x: x.score, reverse=True)

        # Cleanup old dedup entries periodically
        self.dedup.cleanup(max_age_days=30)

        return all_new_items

    def check_feed(self, url: str) -> list:
        """Check a single feed by URL."""
        feed_config = next(
            (f for f in self.feeds if f.url == url),
            FeedConfig(url=url),
        )
        return self._check_feed(feed_config)

    def add_feed(self, url: str, category: str = "general",
                 keywords: Optional[list] = None):
        """Add a new feed to the configuration."""
        new_feed = {
            "url": url,
            "category": category,
            "keywords": keywords or [],
            "importance": "medium",
        }

        # Load existing config
        if self.config_path.exists():
            data = json.loads(self.config_path.read_text())
        else:
            data = {"feeds": []}

        # Check for duplicates
        if any(f["url"] == url for f in data["feeds"]):
            print(f"[rss] Feed already exists: {url}", flush=True)
            return

        data["feeds"].append(new_feed)
        self.config_path.write_text(json.dumps(data, indent=2))
        self.feeds.append(FeedConfig(**new_feed))
        print(f"[rss] Added feed: {url} ({category})", flush=True)

    def remove_feed(self, url: str):
        """Remove a feed from the configuration."""
        if self.config_path.exists():
            data = json.loads(self.config_path.read_text())
            data["feeds"] = [f for f in data["feeds"] if f["url"] != url]
            self.config_path.write_text(json.dumps(data, indent=2))
            self.feeds = [f for f in self.feeds if f.url != url]
            print(f"[rss] Removed feed: {url}", flush=True)

    def _check_feed(self, feed_config: FeedConfig) -> list:
        """Check a single feed for new items matching keywords."""
        content = self._fetch_feed(feed_config.url)
        if not content:
            return []

        items = self.parser.parse(content, feed_config.url)
        new_items = []

        for item in items:
            # Skip if already seen
            if self.dedup.is_seen(item.id):
                continue

            # Apply keyword filter
            item.source_category = feed_config.category
            item.keyword_matches = self._match_keywords(
                item, feed_config.keywords
            )

            # Score the item
            item.score = self._score_item(item, feed_config)

            # Only include items that match keywords (or if no keywords configured)
            if not feed_config.keywords or item.keyword_matches:
                new_items.append(item)

            # Mark as seen regardless of keyword match (dedup)
            self.dedup.mark_seen(item.id, feed_config.url, item.title)

        return new_items

    def _fetch_feed(self, url: str) -> str:
        """Fetch feed content from URL."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Hermes88-RSS/1.0 (Rhodawk AI)",
                "Accept": "application/rss+xml, application/atom+xml, "
                          "application/json, text/xml, */*",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            print(f"[rss] HTTP {e.code} fetching {url}", flush=True)
            return ""
        except urllib.error.URLError as e:
            print(f"[rss] URL error fetching {url}: {e.reason}", flush=True)
            return ""
        except Exception as e:
            print(f"[rss] Fetch error for {url}: {e}", flush=True)
            return ""

    def _match_keywords(self, item: FeedItem, keywords: list) -> list:
        """Check which keywords match in the item."""
        if not keywords:
            return []

        matches = []
        text = f"{item.title} {item.description}".lower()

        for keyword in keywords:
            if keyword.lower() in text:
                matches.append(keyword)

        return matches

    def _score_item(self, item: FeedItem, feed_config: FeedConfig) -> float:
        """Calculate importance score for an item."""
        base_weight = self.IMPORTANCE_WEIGHT.get(feed_config.importance, 2.0)
        keyword_bonus = len(item.keyword_matches) * 1.5
        return base_weight + keyword_bonus

    def synthesize(self, items: list, max_items: int = 5) -> str:
        """
        Synthesize a summary of new feed items.

        Args:
            items: List of new FeedItem objects.
            max_items: Maximum items to include in summary.

        Returns:
            Formatted summary text.
        """
        if not items:
            return "No new items from monitored feeds."

        top_items = items[:max_items]
        lines = [f"RSS Update ({len(items)} new items, showing top {len(top_items)}):"]

        for item in top_items:
            lines.append(f"\n{item.summary()}")
            if item.description:
                lines.append(f"  {item.description[:150]}")
            if item.url:
                lines.append(f"  {item.url}")

        if len(items) > max_items:
            lines.append(f"\n... and {len(items) - max_items} more items")

        return "\n".join(lines)


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for RSS monitor."""
    parser = argparse.ArgumentParser(
        description="RSS Feed Monitor -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # Check
    check_p = sub.add_parser("check", help="Check feeds for new items")
    check_p.add_argument("--all", action="store_true", help="Check all feeds")
    check_p.add_argument("--url", default="", help="Check specific feed URL")
    check_p.add_argument("--max-items", type=int, default=10)

    # Add
    add_p = sub.add_parser("add", help="Add a new feed")
    add_p.add_argument("--url", required=True)
    add_p.add_argument("--category", default="general")
    add_p.add_argument("--keywords", default="", help="Comma-separated keywords")

    # Remove
    remove_p = sub.add_parser("remove", help="Remove a feed")
    remove_p.add_argument("--url", required=True)

    # List
    sub.add_parser("list", help="List configured feeds")

    args = parser.parse_args()
    monitor = RSSMonitor()

    if args.command == "check":
        if args.url:
            items = monitor.check_feed(args.url)
        else:
            items = monitor.check_all()

        if items:
            summary = monitor.synthesize(items, max_items=args.max_items)
            print(summary)
        else:
            print("[rss] No new items matching keywords.")

    elif args.command == "add":
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        monitor.add_feed(args.url, args.category, keywords)

    elif args.command == "remove":
        monitor.remove_feed(args.url)

    elif args.command == "list":
        if monitor.feeds:
            print(f"Configured feeds ({len(monitor.feeds)}):")
            for f in monitor.feeds:
                kw = ", ".join(f.keywords) if f.keywords else "none"
                print(f"  [{f.importance}] {f.category}: {f.url}")
                print(f"    Keywords: {kw}")
        else:
            print("No feeds configured.")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
