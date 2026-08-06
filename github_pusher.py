"""
github_pusher.py — Push built latest.json back to GitHub
=========================================================
After each pipeline run uploads to GCS, this module commits the
new latest.json to the app repo (sample-data/latest.json) so that
the app's fetch chain (which tries GitHub raw first) always gets
today's fresh content sections (opportunity, tip, wisdom) while the
user-data keys (attendance, participants_by_class, invoices, etc.)
that were already in the GitHub file are preserved in the payload
by builder.py before this push happens.

Requires env var:
    GH_TOKEN — a GitHub PAT or fine-grained token with:
                repo → Contents: Read & Write on visitsupri-bot/samavaya-niramaya-app

Usage (standalone):
    python github_pusher.py [--date YYYY-MM-DD]
"""

import argparse
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

GH_REPO   = "visitsupri-bot/samavaya-niramaya-app"
GH_BRANCH = "main"
GH_PATH   = "sample-data/latest.json"
GH_API    = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"

LOCAL_OUTPUT = Path("/tmp/samavaya-daily")


def _get_current_sha(token: str) -> str | None:
    """Return the current blob SHA of the file on GitHub (needed for updates)."""
    req = urllib.request.Request(
        GH_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # file doesn't exist yet — first push
        raise


def push_to_github(target_date: date, token: str) -> None:
    """
    Reads the built latest.json from LOCAL_OUTPUT and commits it to GitHub.
    Preserves the existing file's blob SHA so GitHub accepts the update.
    """
    latest_path = LOCAL_OUTPUT / "latest.json"
    if not latest_path.exists():
        raise FileNotFoundError(
            f"Built output not found: {latest_path}. Run builder.py first."
        )

    content_bytes = latest_path.read_bytes()
    content_b64   = base64.b64encode(content_bytes).decode()

    current_sha = _get_current_sha(token)

    body: dict = {
        "message": f"chore(data): daily update {target_date.isoformat()} [skip ci]",
        "content": content_b64,
        "branch":  GH_BRANCH,
    }
    if current_sha:
        body["sha"] = current_sha  # required when updating an existing file

    req = urllib.request.Request(
        GH_API,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="PUT",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            sha = result.get("content", {}).get("sha", "")[:7]
            print(f"📦 GitHub updated: sample-data/latest.json → {sha}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()
        raise RuntimeError(
            f"GitHub push failed (HTTP {exc.code}): {body_text}"
        ) from exc


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    parser = argparse.ArgumentParser(description="Push built latest.json to GitHub")
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("⚠️  GH_TOKEN not set — skipping GitHub push")
        return

    print(f"📦 Pushing {target_date.isoformat()} latest.json to GitHub...")
    push_to_github(target_date, token)
    print("✅ GitHub push complete")


if __name__ == "__main__":
    main()
