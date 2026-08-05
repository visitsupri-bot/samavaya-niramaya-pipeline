"""
fetcher.py — Samavaya Niramaya Real-Time Trend Fetcher
======================================================
Provides three public functions that enrich the daily opportunity
section with live data from YouTube Data API v3 and Google Trends.

Public API:
    fetch_youtube_trends(api_key, queries, *, max_results=5) -> list[dict]
    fetch_google_trends(keywords)                            -> list[dict]
    fetch_opportunity(api_key, niche)                        -> dict | None

Design constraints:
  - All network calls fail gracefully (return [] or None on any error).
  - YouTube uses the Data API v3 search endpoint (videos ordered by viewCount).
  - Google Trends uses pytrends with a 3-attempt retry / exponential backoff.
  - This module does NOT touch venues[] or differentiation[].
  - The returned opportunity dict contains only:
        market_headline, trend_data, trends[]
"""

import logging
import time
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "config" / "fetch_config.yaml"
_fetch_config: Optional[dict] = None


def _load_fetch_config() -> dict:
    """Load and cache fetch_config.yaml."""
    global _fetch_config
    if _fetch_config is None:
        _fetch_config = yaml.safe_load(_CONFIG_PATH.read_text())
    return _fetch_config


# ── YouTube ───────────────────────────────────────────────

def fetch_youtube_trends(
    api_key: str,
    queries: list[str],
    *,
    max_results: int = 5,
) -> list[dict]:
    """
    Search YouTube for each query and return a flat list of video entries.

    Each entry:
        {
            "title":        str,
            "video_id":     str,
            "channel":      str,
            "published_at": str,   # ISO 8601
            "query":        str,   # originating query
        }

    Returns [] on any error (missing key, quota exceeded, network failure).
    """
    if not api_key:
        logger.info("fetch_youtube_trends: no API key supplied — skipping")
        return []

    try:
        # Lazy import so that environments without the package still work for
        # everything except YouTube fetching.
        from googleapiclient.discovery import build as _yt_build  # type: ignore[import-untyped]
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("google-api-python-client not installed — skipping YouTube fetch")
        return []

    results: list[dict] = []

    try:
        youtube = _yt_build("youtube", "v3", developerKey=api_key, cache_discovery=False)

        for query in queries:
            try:
                response = (
                    youtube.search()
                    .list(
                        q=query,
                        part="snippet",
                        type="video",
                        order="viewCount",
                        maxResults=max_results,
                        relevanceLanguage="en",
                        regionCode="IN",
                    )
                    .execute()
                )
                for item in response.get("items", []):
                    snippet = item.get("snippet", {})
                    video_id = item.get("id", {}).get("videoId", "")
                    results.append(
                        {
                            "title":        snippet.get("title", ""),
                            "video_id":     video_id,
                            "channel":      snippet.get("channelTitle", ""),
                            "published_at": snippet.get("publishedAt", ""),
                            "query":        query,
                        }
                    )
            except HttpError as exc:
                logger.warning("YouTube search failed for query %r: %s", query, exc)
                # Continue with remaining queries (per-query isolation)
                continue

    except Exception as exc:  # noqa: BLE001
        logger.error("fetch_youtube_trends: unexpected error — %s", exc)
        return []

    return results


# ── Google Trends ─────────────────────────────────────────

_TRENDS_MAX_RETRIES = 3
_TRENDS_BACKOFF_BASE = 2.0  # seconds


def fetch_google_trends(keywords: list[str]) -> list[dict]:
    """
    Fetch interest-over-time data from Google Trends for a list of keywords.

    Each entry:
        {
            "keyword":    str,
            "avg_interest": int,    # mean over the timeframe (0–100)
            "peak_interest": int,   # max over the timeframe
            "is_rising":  bool,     # True if last value > first value
        }

    Returns [] if pytrends is unavailable, the request fails after retries,
    or the response DataFrame is empty.
    """
    if not keywords:
        return []

    try:
        from pytrends.request import TrendReq  # type: ignore[import-untyped]
        import pandas as _pd  # pytrends transitively requires pandas
    except ImportError:
        logger.warning("pytrends not installed — skipping Google Trends fetch")
        return []

    for attempt in range(1, _TRENDS_MAX_RETRIES + 1):
        try:
            pt = TrendReq(hl="en-IN", tz=330)  # IST = UTC+330 min
            pt.build_payload(keywords, timeframe="now 7-d", geo="IN")
            df = pt.interest_over_time()

            if df.empty:
                logger.info("fetch_google_trends: empty DataFrame returned")
                return []

            entries: list[dict] = []
            for kw in keywords:
                if kw not in df.columns:
                    continue
                series = df[kw]
                entries.append(
                    {
                        "keyword":       kw,
                        "avg_interest":  int(series.mean()),
                        "peak_interest": int(series.max()),
                        "is_rising":     bool(series.iloc[-1] > series.iloc[0]),
                    }
                )
            return entries

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fetch_google_trends attempt %d/%d failed: %s",
                attempt,
                _TRENDS_MAX_RETRIES,
                exc,
            )
            if attempt < _TRENDS_MAX_RETRIES:
                time.sleep(_TRENDS_BACKOFF_BASE ** attempt)

    logger.error("fetch_google_trends: all %d retries exhausted", _TRENDS_MAX_RETRIES)
    return []


# ── Opportunity assembler ─────────────────────────────────

_MAX_VENUES = 8  # max venues returned per niche


def _format(template: str, trend_topic: str, trend_direction: str) -> str:
    """Safe format — falls back gracefully if template is empty."""
    try:
        return template.format(
            trend_topic=trend_topic,
            trend_direction=trend_direction,
        ).strip()
    except (KeyError, ValueError):
        return template.strip()


def _select_venues(cfg: dict, niche: str, max_venues: int = _MAX_VENUES) -> list[dict]:
    """
    Return venues matching *niche*, geographically interleaved so every region
    (India metros, Middle East, Europe, SE Asia) is represented.
    """
    all_venues = cfg.get("venues", [])
    matched = [v for v in all_venues if niche in v.get("niches", [])]

    # Assign region bucket based on city
    _REGION_ORDER = ["India", "Middle East", "Europe", "SE Asia"]
    _MIDDLE_EAST  = {"Dubai", "Abu Dhabi", "Riyadh", "Doha", "Muscat"}
    _EUROPE       = {"London", "Amsterdam", "Berlin", "Paris", "Zurich", "Barcelona", "Vienna"}
    _SE_ASIA      = {"Bangkok", "Bali", "Singapore", "Phuket", "Chiang Mai", "Koh Samui"}

    def region(v: dict) -> str:
        city = v.get("city", "")
        if city in _MIDDLE_EAST: return "Middle East"
        if city in _EUROPE:      return "Europe"
        if city in _SE_ASIA:     return "SE Asia"
        return "India"

    # Sort India venues: primaries (niche[0] matches) before secondaries, then interleave cities
    india   = sorted(matched, key=lambda v: (0 if v.get("niches", [])[0:1] == [niche] else 1, v.get("city", "")))
    intl_me = [v for v in matched if region(v) == "Middle East"]
    intl_eu = [v for v in matched if region(v) == "Europe"]
    intl_sea= [v for v in matched if region(v) == "SE Asia"]

    # Separate India venues into distinct cities, interleave
    india_by_city: dict[str, list] = {}
    for v in india:
        if region(v) == "India":
            india_by_city.setdefault(v.get("city", "Other"), []).append(v)

    # Round-robin across India cities to ensure spread
    india_interleaved: list[dict] = []
    city_lists = list(india_by_city.values())
    while any(city_lists) and len(india_interleaved) < max_venues:
        for cl in city_lists:
            if cl:
                india_interleaved.append(cl.pop(0))
            if len(india_interleaved) >= max_venues:
                break

    # Interleave: 2 India, 1 intl per cycle until max_venues
    result: list[dict] = []
    ia, me, eu, sea = iter(india_interleaved), iter(intl_me), iter(intl_eu), iter(intl_sea)
    intl_cycle = [me, eu, sea]
    intl_idx   = 0
    india_count = 0
    while len(result) < max_venues:
        if india_count < 2:
            v = next(ia, None)
            if v:
                result.append(v)
                india_count += 1
                continue
        # international turn
        added_intl = False
        for _ in range(len(intl_cycle)):
            v = next(intl_cycle[intl_idx % len(intl_cycle)], None)
            intl_idx += 1
            if v:
                result.append(v)
                added_intl = True
                break
        india_count = 0
        if not added_intl:
            # exhaust remaining India venues
            v = next(ia, None)
            if v:
                result.append(v)
            else:
                break

    return [
        {k: val for k, val in venue.items() if k != "niches"}
        for venue in result[:max_venues]
    ]


def _clean_topic(query: str) -> str:
    """Extract a clean 2-3 word topic from a YouTube search query."""
    # Remove trailing years, country names, filler words
    stop = {"india", "2025", "2026", "hindi", "english", "for", "and", "the",
            "with", "how", "to", "a", "an", "in", "of", "on", "by", "at"}
    words = [w for w in query.lower().split() if w not in stop]
    # Capitalise first 3 meaningful words
    return " ".join(w.capitalize() for w in words[:3]) or query.split()[0].capitalize()


def _make_hashtags(queries: list[str], count: int = 5) -> list[str]:
    """Build clean, readable hashtags from search queries."""
    seen: set[str] = set()
    tags: list[str] = []
    for q in queries:
        # Take first 3 meaningful words, join camelCase
        words = [w for w in q.lower().split()
                 if w not in {"india", "2025", "2026", "hindi", "for", "and", "the", "a", "an"}]
        tag = "#" + "".join(w.capitalize() for w in words[:3])
        if tag not in seen and len(tag) < 28:
            seen.add(tag)
            tags.append(tag)
        if len(tags) >= count:
            break
    return tags


def fetch_opportunity(
    api_key: str,
    niche: str,
    *,
    market_context: str = "",
) -> Optional[dict]:
    """
    Assemble a cohesive opportunity dict where ALL 4 trend cards relate to the
    same active niche (set by the weekly market theme in content.yaml).

    Each card covers a DIFFERENT query from the niche's query list so they
    each have a distinct topic. Cards:
      1. YouTube India      — primary yoga/wellness search demand
      2. YouTube Niche      — second distinct query (kids/sound/corporate/retreat angle)
      3. Instagram India    — Reels/short-form content discovery angle
      4. Google Trends      — search volume signal (or descriptive fallback)

    market_context is the strategic 'why' from the weekly theme — shown as
    the opportunity insight on Card 1 (the most important card).

    Venues are filtered by the active niche so the whole tab is coherent:
    corporate_wellness niche → corporate venues; sound_healing → retreat venues; etc.
    """
    cfg = _load_fetch_config()
    niche_cfg = cfg.get("niches", {}).get(niche)
    if not niche_cfg:
        logger.warning("fetch_opportunity: unknown niche %r -- skipping", niche)
        return None

    youtube_queries   = niche_cfg.get("youtube_queries", [])
    instagram_queries = niche_cfg.get("instagram_queries", [])
    trends_keywords   = niche_cfg.get("google_trends_keywords", [])
    platform_insights = niche_cfg.get("platform_insights", {})

    # Assign distinct queries to each card so topics differ
    # Card 1 = first primary query, Card 2 = second primary query (or instagram_queries[0])
    card1_queries = youtube_queries[0:2] if youtube_queries else []
    card2_queries = youtube_queries[2:4] if len(youtube_queries) > 2 else instagram_queries[0:2]
    card3_queries = instagram_queries if instagram_queries else youtube_queries[4:6]

    # Fetch live data — each batch uses its own focused queries
    yt1_results = fetch_youtube_trends(api_key, card1_queries, max_results=5)
    yt2_results = fetch_youtube_trends(api_key, card2_queries, max_results=5)
    ig_results  = fetch_youtube_trends(api_key, card3_queries, max_results=5)
    trend_results = fetch_google_trends(trends_keywords)

    if not yt1_results and not yt2_results and not ig_results and not trend_results:
        logger.info("fetch_opportunity: all sources empty for niche %r -- returning None", niche)
        return None

    # Overall trend direction from Google Trends if available, else assume upward
    is_rising = trend_results[0].get("is_rising", True) if trend_results else True
    trend_direction = "upward" if is_rising else "stable"

    trends: list[dict] = []

    # ── Card 1: YouTube India — primary niche demand signal ──
    c1_query = card1_queries[0] if card1_queries else niche.replace("_", " ")
    c1_topic = _clean_topic(c1_query)
    c1_count = len(yt1_results)
    c1_headline = (
        f"{c1_count} videos found for '{c1_topic}' — viewer demand is {trend_direction} on YouTube India"
        if yt1_results else
        f"YouTube India: '{c1_topic}' content demand is {trend_direction}"
    )
    c1_hashtags = _make_hashtags(card1_queries + [c1_query])
    # Use market_context as the opportunity insight for Card 1 (the strategic 'why')
    c1_insight = market_context or _format(
        platform_insights.get("youtube", ""), c1_topic, trend_direction
    ) or f"'{c1_topic}' is {trend_direction} on YouTube India — authentic teacher-led content commands highest watch time."

    trends.append({
        "platform":    "YouTube India",
        "headline":    c1_headline,
        "hashtags":    c1_hashtags[:5],
        "opportunity": c1_insight,
    })

    # ── Card 2: YouTube — second distinct niche angle ─────
    c2_query = card2_queries[0] if card2_queries else (youtube_queries[1] if len(youtube_queries) > 1 else c1_query)
    c2_topic = _clean_topic(c2_query)
    c2_count = len(yt2_results)
    c2_headline = (
        f"{c2_count} videos found for '{c2_topic}' — {trend_direction} demand on YouTube India"
        if yt2_results else
        f"YouTube India: '{c2_topic}' content is {trend_direction}"
    )
    c2_hashtags = _make_hashtags(card2_queries)
    c2_insight = _format(
        platform_insights.get("youtube_kids_sound", "") or platform_insights.get("youtube", ""),
        c2_topic, trend_direction
    ) or f"'{c2_topic}' is an underserved niche on YouTube India with {trend_direction} viewer demand."

    trends.append({
        "platform":    f"YouTube — {c2_topic}",
        "headline":    c2_headline,
        "hashtags":    c2_hashtags[:5],
        "opportunity": c2_insight,
    })

    # ── Card 3: Instagram India — Reels discovery angle ───
    c3_query = card3_queries[0] if card3_queries else c1_query
    c3_topic = _clean_topic(c3_query)
    c3_count = len(ig_results)
    # Instagram hashtags come from Google Trends keywords (actual search terms people use)
    ig_hashtags = ["#" + kw.replace(" ", "").title()[:20] for kw in trends_keywords[:5]]
    if not ig_hashtags:
        ig_hashtags = _make_hashtags(card3_queries)
    c3_headline = (
        f"Instagram India: '{c3_topic}' Reels are {trend_direction} "
        f"— {c3_count} videos confirm short-form demand"
        if ig_results else
        f"Instagram India: '{c3_topic}' Reels content is {trend_direction} — content gap available"
    )
    c3_insight = _format(
        platform_insights.get("instagram", ""), c3_topic, trend_direction
    ) or f"Short-form '{c3_topic}' content on Instagram Reels drives organic discovery — zero ad spend required."

    trends.append({
        "platform":    "Instagram India",
        "headline":    c3_headline,
        "hashtags":    ig_hashtags[:5],
        "opportunity": c3_insight,
    })

    # ── Card 4: Google Trends India — search demand signal ─
    gt_hashtags = ["#" + kw.replace(" ", "").title()[:20] for kw in trends_keywords[:5]]
    if trend_results:
        gt_kw   = trend_results[0].get("keyword", c1_topic)
        peak    = trend_results[0].get("peak_interest", 0)
        avg     = trend_results[0].get("avg_interest", 0)
        gt_headline = (
            f"'{gt_kw.title()}' searches are {trend_direction} in India "
            f"— peak {peak}/100, avg {avg}/100 this week"
        )
        gt_insight = _format(
            platform_insights.get("google_trends", ""), gt_kw.title(), trend_direction
        ) or f"Search demand for '{gt_kw}' is {trend_direction} — strong organic discovery opportunity across Tier-1 cities."
    else:
        # Fallback: describe what the searches tell us without Google Trends data
        search_themes = " · ".join(f"'{kw}'" for kw in trends_keywords[:3])
        gt_headline = (
            f"India searches for {search_themes} are {trend_direction} "
            f"— niche demand outpacing generic wellness content"
        )
        gt_insight = _format(
            platform_insights.get("google_trends", ""), c1_topic, trend_direction
        ) or f"Search demand for {c1_topic} is {trend_direction} across India — condition-specific content commands strong pricing power."

    trends.append({
        "platform":    "Google Trends India",
        "headline":    gt_headline,
        "hashtags":    gt_hashtags[:5],
        "opportunity": gt_insight,
    })

    # Venues filtered by the ACTIVE NICHE so they match the market theme
    venues = _select_venues(cfg, niche)

    return {
        "market_headline":       "",   # set by rotate_payload from weekly theme — not here
        "_live_market_headline": True,  # sentinel: prevents static rotation from overwriting
        "trends":                trends,
        "venues":                venues,
        "trend_data": {
            "youtube_count": len(yt1_results) + len(yt2_results) + len(ig_results),
            "trends_count":  len(trend_results),
        },
    }
