"""
builder.py — Samavaya Niramaya Daily JSON Builder
==================================================
Assembles today's daily payload by:
  - Rotating the featured condition by day-of-year mod len(conditions)
  - Rotating the featured wisdom verses by day-of-year mod len(sources)
  - Embedding schedule from config/schedule.yaml
  - Writing output to /tmp/daily/<YYYY-MM-DD>.json and /tmp/daily/latest.json

Usage:
    python builder.py [--date YYYY-MM-DD] [--source-json PATH]
"""

import json
import argparse
import copy
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import yaml  # PyYAML — see requirements.txt

try:
    import fetcher as _fetcher
except ImportError:  # pragma: no cover
    _fetcher = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CONFIG_DIR   = BASE_DIR / "config"
OUTPUT_DIR   = Path("/tmp/samavaya-daily")
TEMPLATE_DIR = BASE_DIR / "resources"


def load_config() -> dict:
    content  = yaml.safe_load((CONFIG_DIR / "content.yaml").read_text())
    schedule = yaml.safe_load((CONFIG_DIR / "schedule.yaml").read_text())
    return {"content": content, "schedule": schedule}


def pick_by_day(items: list, day_of_year: int) -> object:
    """Round-robin selection keyed to day-of-year."""
    if not items:
        return None
    return items[day_of_year % len(items)]


def build_schedule_section(sched_cfg: dict) -> dict:
    return {
        "classes": sched_cfg.get("classes", []),
        "week_theme":       sched_cfg.get("week_theme", ""),
        "week_ref":         sched_cfg.get("week_ref", ""),
        "pranayama":        sched_cfg.get("pranayama", ""),
        "sound_frequency":  sched_cfg.get("sound_frequency", ""),
        "chakra":           sched_cfg.get("chakra", ""),
        "instrument":       sched_cfg.get("instrument", ""),
        "sound_duration":   sched_cfg.get("sound_duration", ""),
    }


def rotate_payload(payload: dict, day_of_year: int, config: dict) -> dict:
    """
    Mutates a deep copy of payload to rotate:
      - sections.tip.featured_condition  (by day-of-year mod 14)
      - sections.wisdom source ordering  (lead source by day-of-year mod 4)
    """
    p = copy.deepcopy(payload)

    # Rotate featured condition
    conditions = config["content"].get("featured_conditions", [])
    if conditions and "tip" in p.get("sections", {}):
        p["sections"]["tip"]["featured_condition"] = pick_by_day(conditions, day_of_year)

    # Rotate wisdom lead source (first in list = default selected)
    wisdom_sources = config["content"].get("wisdom_sources", [])
    if wisdom_sources and "wisdom" in p.get("sections", {}):
        rotated_source = pick_by_day(wisdom_sources, day_of_year)
        p["sections"]["wisdom"]["_featured_source"] = rotated_source

    # Rotate market headline — only if not already set by live fetcher
    headlines = config["content"].get("market_headlines", [])
    opp = p.get("sections", {}).get("opportunity", {})
    live_headline = opp.get("_live_market_headline")
    if headlines and "opportunity" in p.get("sections", {}) and not live_headline:
        p["sections"]["opportunity"]["market_headline"] = pick_by_day(headlines, day_of_year)

    # Inject schedule from config
    p["sections"]["schedule"] = {
        **p["sections"].get("schedule", {}),
        **build_schedule_section(config["schedule"]),
    }

    return p


def build(target_date: date, source_json: Path | None) -> dict:
    config = load_config()
    day_of_year = target_date.timetuple().tm_yday

    # Load template / source payload
    if source_json and source_json.exists():
        base = json.loads(source_json.read_text())
    elif TEMPLATE_DIR.exists():
        # Look for most recent dated JSON in resources/
        jsons = sorted(TEMPLATE_DIR.glob("*.json"), reverse=True)
        if jsons:
            base = json.loads(jsons[0].read_text())
        else:
            raise FileNotFoundError("No source JSON found in resources/")
    else:
        raise FileNotFoundError(
            "Provide --source-json or place a dated JSON in resources/"
        )

    # Stamp metadata
    base["generated_at"] = datetime.now(timezone.utc).isoformat()
    base["date"] = target_date.isoformat()

    # ── Optional: enrich opportunity section with live trend data ──
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if _fetcher is not None and youtube_api_key:
        niche = config["content"].get("default_niche", "yoga_india")
        logger.info("Fetching live opportunity data for niche=%r", niche)
        try:
            opportunity_data = _fetcher.fetch_opportunity(youtube_api_key, niche)
            if opportunity_data:
                base.setdefault("sections", {}).setdefault("opportunity", {}).update(
                    opportunity_data
                )
                logger.info(
                    "Opportunity section enriched: %d YT results, %d trend entries",
                    opportunity_data.get("trend_data", {}).get("youtube_count", 0),
                    opportunity_data.get("trend_data", {}).get("trends_count", 0),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_opportunity failed, continuing without live data: %s", exc)
    else:
        if _fetcher is None:
            logger.debug("fetcher module unavailable — skipping live trend enrichment")
        else:
            logger.info("YOUTUBE_API_KEY not set — skipping live trend enrichment")

    return rotate_payload(base, day_of_year, config)


def write_output(payload: dict, target_date: date) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dated_path  = OUTPUT_DIR / f"{target_date.isoformat()}.json"
    latest_path = OUTPUT_DIR / "latest.json"
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    dated_path.write_text(data)
    latest_path.write_text(data)
    print(f"✅ Written: {dated_path}")
    print(f"✅ Written: {latest_path}")


def main():
    parser = argparse.ArgumentParser(description="Build Samavaya Niramaya daily JSON")
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--source-json", default=None, help="Path to base JSON template")
    args = parser.parse_args()

    target_date = (
        date.fromisoformat(args.date) if args.date else date.today()
    )
    source_path = Path(args.source_json) if args.source_json else None

    print(f"🪷 Building payload for {target_date.isoformat()} (day {target_date.timetuple().tm_yday})")
    payload = build(target_date, source_path)
    write_output(payload, target_date)


if __name__ == "__main__":
    main()
