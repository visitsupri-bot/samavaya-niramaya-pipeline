"""
uploader.py — Samavaya Niramaya GCS Uploader
=============================================
Uploads the built daily JSON files to GCS bucket:
    gs://samavaya-niramaya/daily/<YYYY-MM-DD>.json
    gs://samavaya-niramaya/daily/latest.json

Requires: google-cloud-storage (see requirements.txt)
Auth: Application Default Credentials (service account on Cloud Run)

Usage:
    python uploader.py [--date YYYY-MM-DD] [--bucket BUCKET_NAME]
"""

import argparse
import json
from datetime import date
from pathlib import Path

from google.cloud import storage  # type: ignore


BUCKET_NAME  = "samavaya-niramaya"
GCS_PREFIX   = "daily"
LOCAL_OUTPUT = Path("/tmp/samavaya-daily")


def upload_file(client: storage.Client, bucket_name: str, local_path: Path, gcs_blob: str) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(gcs_blob)
    blob.content_type = "application/json"
    blob.cache_control = "public, max-age=3600"

    blob.upload_from_filename(str(local_path))
    blob.make_public()  # Allow unauthenticated reads for PWA
    print(f"☁️  Uploaded gs://{bucket_name}/{gcs_blob}")


def upload(target_date: date, bucket_name: str) -> None:
    client = storage.Client()

    dated_local  = LOCAL_OUTPUT / f"{target_date.isoformat()}.json"
    latest_local = LOCAL_OUTPUT / "latest.json"

    if not dated_local.exists():
        raise FileNotFoundError(f"Build output not found: {dated_local}. Run builder.py first.")

    upload_file(client, bucket_name, dated_local,  f"{GCS_PREFIX}/{target_date.isoformat()}.json")
    upload_file(client, bucket_name, latest_local, f"{GCS_PREFIX}/latest.json")


def main():
    parser = argparse.ArgumentParser(description="Upload Samavaya Niramaya daily JSON to GCS")
    parser.add_argument("--date",   default=None, help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--bucket", default=BUCKET_NAME, help=f"GCS bucket name (default: {BUCKET_NAME})")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    print(f"☁️  Uploading {target_date.isoformat()} to gs://{args.bucket}/{GCS_PREFIX}/")
    upload(target_date, args.bucket)
    print("✅ Upload complete")


if __name__ == "__main__":
    main()
