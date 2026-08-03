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

def fetch_opportunity(api_key: str, niche: str) -> Optional[dict]:
    """
    Assemble a partial opportunity dict for *niche* using live trend data.

    Returns a dict with keys:
        market_headline  str   — human-readable headline derived from top trend
        trend_data       dict  — raw summary {"youtube_count": int, "trends_count": int}
        trends           list  — Google Trends entries (may be [])

    Returns None if both YouTube and Google Trends return empty results (nothing
    to enrich with), or if the niche is not found in fetch_config.yaml.

    Does NOT touch venues[] or differentiation[].
    """
    cfg = _load_fetch_config()
    niche_cfg = cfg.get("niches", {}).get(niche)
    if not niche_cfg:
        logger.warning("fetch_opportunity: unknown niche %r — skipping", niche)
        return None

    youtube_queries      = niche_cfg.get("youtube_queries", [])
    trends_keywords      = niche_cfg.get("google_trends_keywords", [])
    opportunity_template = niche_cfg.get("opportunity_template", "")

    # ── Fetch in parallel (sequential here, kept simple) ──
    yt_results    = fetch_youtube_trends(api_key, youtube_queries)
    trend_results = fetch_google_trends(trends_keywords)

    if not yt_results and not trend_results:
        logger.info(
            "fetch_opportunity: both sources empty for niche %r — returning None", niche
        )
        return None

    # ── Derive headline ────────────────────────────────────
    top_yt_title = yt_results[0]["title"] if yt_results else ""
    top_trend    = trend_results[0] if trend_results else {}

    trend_topic     = top_trend.get("keyword", top_yt_title) or niche.replace("_", " ")
    is_rising       = top_trend.get("is_rising", True)
    trend_direction = "upward" if is_rising else "stable"

    market_headline = opportunity_template.format(
        trend_topic=trend_topic,
        trend_direction=trend_direction,
    ).strip()

    if not market_headline:
        # Fallback if template is empty
        market_headline = f"{trend_topic} interest is {trend_direction}"

    # ── Tags from top YouTube result ───────────────────────
    # We only have snippet data (no tags[] in search response); use titles as
    # a proxy.  The field is informational only.
    top_yt_tag = yt_results[0]["query"] if yt_results else niche

    return {
        "market_headline": market_headline,
        "trend_data": {
            "youtube_count": len(yt_results),
            "trends_count":  len(trend_results),
        },
        "trends": trend_results,
        # Convenience field used downstream if callers want a search label
        "_top_yt_tag": top_yt_tag,
    }
