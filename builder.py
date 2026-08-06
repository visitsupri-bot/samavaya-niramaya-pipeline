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

import urllib.error
import urllib.request

import yaml  # PyYAML — see requirements.txt

try:
    import fetcher as _fetcher
except ImportError:  # pragma: no cover
    _fetcher = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ── User-data preservation ────────────────────────────────
# Keys owned exclusively by the app — pipeline never overwrites these
USER_DATA_KEYS = [
    'participants', 'participants_by_class', 'attendance', 'invoices', 'venues',
    'week_overrides', 'venue_pipeline', 'wisdom_favourites',
]

GCS_LATEST_URL = 'https://storage.googleapis.com/samavaya-niramaya/daily/latest.json'
GITHUB_RAW_URL = (
    'https://raw.githubusercontent.com/visitsupri-bot/samavaya-niramaya-app'
    '/main/sample-data/latest.json'
)


def fetch_live_user_data() -> dict:
    """
    Fetches the currently-live latest.json from GitHub raw first (where the app's
    Save button writes to), falling back to GCS. Returns only user-data section keys.

    Rejects any source whose participant data looks like the original sample/placeholder
    data (par_001 / "Ananya S." etc.) — so the pipeline can never propagate fake names.
    Returns an empty dict if no real data is reachable.
    """
    # Sentinel: IDs and names that identify the original sample dataset.
    # If ALL participants from a source match these, the data is fake — skip it.
    SAMPLE_IDS   = {'par_001', 'par_002', 'par_003', 'par_004', 'par_005', 'par_006', 'par_007'}
    SAMPLE_NAMES = {'Ananya S.', 'Rohan M.', 'Priya K.', 'Vijay T.', 'Meera L.', 'Aditya R.', 'Sunita P.'}

    def _is_sample(sections: dict) -> bool:
        """Return True if the participants look like the original placeholder data."""
        # Check flat participants list
        flat = sections.get('participants', [])
        if flat:
            ids   = {p.get('id')   for p in flat if isinstance(p, dict)}
            names = {p.get('name') for p in flat if isinstance(p, dict)}
            if ids and ids.issubset(SAMPLE_IDS) and names and names.issubset(SAMPLE_NAMES):
                return True
        # Check per-class map
        pbc = sections.get('participants_by_class', {})
        if pbc:
            all_ids = {
                p.get('id')
                for lst in pbc.values() if isinstance(lst, list)
                for p in lst if isinstance(p, dict)
            }
            if all_ids and all_ids.issubset(SAMPLE_IDS):
                return True
        return False

    for url in (GITHUB_RAW_URL, GCS_LATEST_URL):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                live = json.loads(resp.read().decode())
                sections = live.get('sections', {})
                if _is_sample(sections):
                    logger.warning(
                        'Skipping %s — participant data looks like sample/placeholder data', url
                    )
                    continue
                return {k: sections[k] for k in USER_DATA_KEYS if k in sections}
        except Exception as exc:  # noqa: BLE001
            logger.warning('Could not fetch live user data from %s: %s', url, exc)
    logger.warning('No real user data found in any source — user-data keys will use template values')
    return {}


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

    # Apply weekly market theme — always overrides whatever the fetcher put there
    # The weekly theme (headline + context) is the authoritative Market Radar signal
    themes = config["content"].get("market_themes", [])
    if themes and "opportunity" in p.get("sections", {}):
        week_of_year = (day_of_year - 1) // 7
        theme = themes[week_of_year % len(themes)]
        p["sections"]["opportunity"]["market_headline"] = theme["headline"]
        p["sections"]["opportunity"]["market_context"]  = theme.get("context", "")

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

    # ── Derive active niche from weekly market theme ───────
    themes = config["content"].get("market_themes", [])
    week_of_year = (day_of_year - 1) // 7
    active_theme = themes[week_of_year % len(themes)] if themes else {}
    active_niche   = active_theme.get("niche", "yoga_india")
    active_context = active_theme.get("context", "")
    logger.info("Active market theme week=%d niche=%r", week_of_year, active_niche)

    # ── Enrich opportunity section with live trend data ────
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if _fetcher is not None and youtube_api_key:
        logger.info("Fetching live opportunity data for niche=%r", active_niche)
        try:
            opportunity_data = _fetcher.fetch_opportunity(
                youtube_api_key, active_niche, market_context=active_context
            )
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

    # Preserve user-data keys from live JSON so pipeline never overwrites app edits
    # NOTE: must happen AFTER opportunity enrichment so live YouTube data is not clobbered
    live_user_data = fetch_live_user_data()
    if live_user_data:
        s = base.setdefault('sections', {})
        for k, v in live_user_data.items():
            s[k] = v  # overwrite each user-data key individually (never touches opportunity)
        logger.info('Preserved user-data keys from live JSON: %s', list(live_user_data.keys()))
    else:
        logger.warning(
            'No live user data found — pipeline will use template values for user-data keys'
        )

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
