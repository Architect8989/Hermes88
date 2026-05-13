# Skill: rss-monitor (Peak v1.0)

## Purpose
Monitor RSS feeds for competitive intelligence, industry news,
security advisories, and technology updates.

## When This Skill Applies
- Proactive: check feeds every 4 hours
- "What's new in [topic]?"
- "Add [URL] to my RSS feeds"
- "Remove feed [name]"

## Feed Configuration
Stored at /data/.hermes/config/rss_feeds.json:
```json
{
  "feeds": [
    {
      "url": "https://github.blog/feed/",
      "category": "github",
      "keywords": ["security", "actions", "copilot"],
      "importance": "high"
    },
    {
      "url": "https://blog.cloudflare.com/rss/",
      "category": "security",
      "keywords": ["zero-day", "DDoS", "WAF"],
      "importance": "medium"
    }
  ]
}
```

## Check feeds
python3 /app/skills/rss-monitor/rss_client.py check --all

## Add feed
python3 /app/skills/rss-monitor/rss_client.py add \
  --url "https://example.com/feed" \
  --category "category" \
  --keywords "key1,key2"

## Remove feed
python3 /app/skills/rss-monitor/rss_client.py remove --url "https://example.com/feed"

## Protocol
1. Fetch all configured feeds
2. Parse entries (handle RSS 2.0, Atom, JSON Feed)
3. Filter by keywords (title + description match)
4. Deduplicate against last-seen entries (stored in SQLite)
5. Score by importance (feed importance * keyword match count)
6. Top 5 new items: synthesize summary for operator
7. Store all seen items for dedup

## Cron Schedule
0 */4 * * * (every 4 hours)

## Error Handling
- If feed unreachable: skip, retry next cycle
- If feed format invalid: log warning, skip
- If too many new items (>20): summarize top 5 only
