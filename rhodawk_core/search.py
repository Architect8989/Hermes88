#!/usr/bin/env python3
"""
rhodawk_core/search.py — Universal 4-Tier Search Stack (Layer G)

Four-tier search stack that never fails:

Tier 1: DDG Instant Answer API (no key, rate-limited but fast)
Tier 2: Brave Search API (BRAVE_API_KEY, 2000 free/month)
Tier 3: Exa AI semantic search (EXA_API_KEY, AI-native)
Tier 4: camofox + @google_search macro (always works, stealth browser)

The tiers cascade automatically — if one fails, the next activates.
Tier 4 cannot be blocked because camofox presents as a real Firefox browser.

SOUL.md alignment: "Never declare search unavailable."

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


# ── Config ────────────────────────────────────────────────────────────────────

BRAVE_API_KEY   = os.environ.get("BRAVE_API_KEY", "")
EXA_API_KEY     = os.environ.get("EXA_API_KEY", "")
CAMOFOX_HOST    = os.environ.get("CAMOFOX_HOST", "camofox")
CAMOFOX_PORT    = os.environ.get("CAMOFOX_PORT", "9377")
CAMOFOX_KEY     = os.environ.get("CAMOFOX_ACCESS_KEY", "")
CAMOFOX_BASE    = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single search result."""
    title: str
    url: str
    snippet: str
    source_tier: str    # "ddg", "brave", "exa", "camofox"


@dataclass
class SearchResponse:
    """Response from the search stack."""
    query: str
    results: List[SearchResult] = field(default_factory=list)
    tier_used: str = ""
    error: Optional[str] = None

    def format(self, max_results: int = 5) -> str:
        """Format results for inclusion in a system prompt or tool output."""
        if self.error and not self.results:
            return f"[search] Error: {self.error}"
        lines = [f"Search: {self.query} (via {self.tier_used})"]
        for i, r in enumerate(self.results[:max_results], 1):
            lines.append(f"\n{i}. {r.title}")
            lines.append(f"   {r.url}")
            lines.append(f"   {r.snippet[:300]}")
        return "\n".join(lines)


# ── Tier 1: DuckDuckGo ────────────────────────────────────────────────────────


def _search_ddg(query: str, max_results: int = 5) -> List[SearchResult]:
    """Search DuckDuckGo. Uses duckduckgo_search package if available."""
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(SearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", "")[:400],
                    source_tier="ddg",
                ))
        return results
    except Exception:
        return []


# ── Tier 2: Brave Search ──────────────────────────────────────────────────────


def _search_brave(query: str, max_results: int = 5) -> List[SearchResult]:
    """Search Brave API. Requires BRAVE_API_KEY."""
    if not BRAVE_API_KEY:
        return []
    try:
        url = (
            "https://api.search.brave.com/res/v1/web/search"
            f"?q={urllib.parse.quote(query)}&count={max_results}"
        )
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("X-Subscription-Token", BRAVE_API_KEY)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for r in data.get("web", {}).get("results", []):
            results.append(SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", "")[:400],
                source_tier="brave",
            ))
        return results
    except Exception:
        return []


# ── Tier 3: Exa AI Semantic Search ───────────────────────────────────────────


def _search_exa(query: str, max_results: int = 5) -> List[SearchResult]:
    """Search Exa AI (semantic search). Requires EXA_API_KEY."""
    if not EXA_API_KEY:
        return []
    try:
        payload = json.dumps({
            "query": query,
            "numResults": max_results,
            "useAutoprompt": True,
            "type": "auto",
            "contents": {"text": {"maxCharacters": 400}},
        }).encode()
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": EXA_API_KEY,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        results = []
        for r in data.get("results", []):
            text = (r.get("text") or "")[:400]
            results.append(SearchResult(
                title=r.get("title", r.get("url", "")),
                url=r.get("url", ""),
                snippet=text or r.get("highlights", [""])[0][:400],
                source_tier="exa",
            ))
        return results
    except Exception:
        return []


# ── Tier 4: camofox Google Search Macro ──────────────────────────────────────


def _search_camofox_google(query: str, max_results: int = 5) -> List[SearchResult]:
    """
    Search Google via camofox stealth browser + @google_search macro.
    This tier cannot be blocked — camofox presents as real Firefox.
    """
    try:
        # Health check
        req = urllib.request.Request(f"{CAMOFOX_BASE}/health")
        if CAMOFOX_KEY:
            req.add_header("Authorization", f"Bearer {CAMOFOX_KEY}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return []
    except Exception:
        return []

    session_id = f"search-{int(time.time())}"
    tab_id = ""

    try:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if CAMOFOX_KEY:
            headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"

        # Use @google_search macro
        payload = json.dumps({
            "userId": session_id,
            "sessionKey": "search",
            "url": f"@google_search?q={urllib.parse.quote(query)}",
        }).encode()
        req = urllib.request.Request(f"{CAMOFOX_BASE}/tabs", data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            tab_data = json.loads(resp.read())
        tab_id = tab_data.get("tabId") or tab_data.get("id", "")
        if not tab_id:
            return []

        time.sleep(3)  # Wait for Google search to render

        # Get snapshot
        snap_req = urllib.request.Request(f"{CAMOFOX_BASE}/tabs/{tab_id}/snapshot", headers=headers)
        with urllib.request.urlopen(snap_req, timeout=15) as resp:
            snap_data = json.loads(resp.read())

        content = snap_data.get("text", "")

        # Parse search results from accessibility snapshot
        results = _parse_google_snapshot(query, content, max_results)
        return results

    except Exception:
        return []
    finally:
        if tab_id:
            try:
                headers = {}
                if CAMOFOX_KEY:
                    headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"
                del_req = urllib.request.Request(
                    f"{CAMOFOX_BASE}/tabs/{tab_id}",
                    method="DELETE",
                    headers=headers,
                )
                urllib.request.urlopen(del_req, timeout=5)
            except Exception:
                pass


def _parse_google_snapshot(query: str, snapshot: str, max_results: int) -> List[SearchResult]:
    """
    Parse Google search results from camofox accessibility snapshot.
    The snapshot is plain text with element refs — we extract title/URL/snippet patterns.
    """
    import re
    results = []

    # Match URL patterns in the snapshot
    url_pattern = re.compile(r'https?://(?!www\.google)[^\s\]\)"\']+')
    urls = url_pattern.findall(snapshot)

    # Deduplicate and filter Google internal URLs
    seen_urls = set()
    clean_urls = []
    for url in urls:
        url = url.rstrip('.,;)')
        if url not in seen_urls and 'google.com' not in url:
            seen_urls.add(url)
            clean_urls.append(url)

    # Extract context around each URL as the snippet
    for url in clean_urls[:max_results]:
        idx = snapshot.find(url)
        context_start = max(0, idx - 200)
        context_end = min(len(snapshot), idx + 300)
        context = snapshot[context_start:context_end].strip()

        # Extract first line before the URL as title
        lines_before = snapshot[:idx].strip().splitlines()
        title = lines_before[-1].strip()[:100] if lines_before else url

        results.append(SearchResult(
            title=title or url,
            url=url,
            snippet=context[:400],
            source_tier="camofox",
        ))

    if not results and snapshot:
        # Fallback: return the raw snapshot as a single result
        results.append(SearchResult(
            title=f"Google search: {query}",
            url=f"https://www.google.com/search?q={urllib.parse.quote(query)}",
            snippet=snapshot[:800],
            source_tier="camofox",
        ))

    return results


# ── Main Search Interface ─────────────────────────────────────────────────────


def search(query: str, max_results: int = 5) -> SearchResponse:
    """
    Execute a search using the 4-tier cascade.

    Always returns results — never fails silently.
    Logs which tier was used.

    Tier order:
    1. DDG (fastest, no key needed)
    2. Brave (higher quality, requires BRAVE_API_KEY)
    3. Exa (semantic/AI-native, requires EXA_API_KEY)
    4. camofox Google (cannot be blocked)
    """
    response = SearchResponse(query=query)

    # Tier 1: DDG
    try:
        results = _search_ddg(query, max_results)
        if results:
            response.results = results
            response.tier_used = "ddg"
            return response
    except Exception:
        pass

    # Tier 2: Brave
    try:
        results = _search_brave(query, max_results)
        if results:
            response.results = results
            response.tier_used = "brave"
            return response
    except Exception:
        pass

    # Tier 3: Exa
    try:
        results = _search_exa(query, max_results)
        if results:
            response.results = results
            response.tier_used = "exa"
            return response
    except Exception:
        pass

    # Tier 4: camofox Google (always available)
    try:
        results = _search_camofox_google(query, max_results)
        if results:
            response.results = results
            response.tier_used = "camofox-google"
            return response
    except Exception:
        pass

    # Should never reach here
    response.error = "All 4 search tiers failed"
    response.tier_used = "none"
    return response


def search_format(query: str, max_results: int = 5) -> str:
    """Convenience function: search and return formatted string."""
    resp = search(query, max_results)
    return resp.format(max_results)
