"""
generate.py — Samavaya Niramaya Pipeline Entrypoint
====================================================
Orchestrates the full daily pipeline:
    1. builder.py  — assemble + rotate daily JSON
    2. uploader.py — push to GCS

This is the entrypoint called by the Cloud Run Job.

Usage:
    python generate.py [--date YYYY-MM-DD] [--source-json PATH] [--bucket BUCKET]
    python generate.py --skip-upload   # local dev, no GCS
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import builder
import uploader

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')


SEP = "=" * 55


def main():
    parser = argparse.ArgumentParser(description="Samavaya Niramaya — full daily pipeline")
    parser.add_argument("--date",        default=None, help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--source-json", default=None, help="Base JSON template path")
    parser.add_argument("--bucket",      default=uploader.BUCKET_NAME, help="GCS bucket name")
    parser.add_argument("--skip-upload", action="store_true", help="Build only, skip GCS upload (local dev)")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    source_path = Path(args.source_json) if args.source_json else None

    print("")
    print(SEP)
    print("  🪷 Samavaya Niramaya Pipeline — " + target_date.isoformat())
    print(SEP)

    # Step 1: Build
    print("")
    print("[1/2] Building daily payload...")
    payload = builder.build(target_date, source_path)
    builder.write_output(payload, target_date)

    # Step 2: Upload (unless --skip-upload)
    if args.skip_upload:
        print("")
        print("[2/2] Skipping upload (--skip-upload flag set)")
    else:
        print("")
        print("[2/2] Uploading to GCS...")
        try:
            uploader.upload(target_date, args.bucket)
        except Exception as exc:
            print("Upload failed: " + str(exc), file=sys.stderr)
            sys.exit(1)

    print("")
    print("Pipeline complete for " + target_date.isoformat())
    print("")


if __name__ == "__main__":
    main()
