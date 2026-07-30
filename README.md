# 🪷 Samavaya Niramaya — Pipeline

Daily JSON content pipeline for the [Samavaya Niramaya PWA](../samavaya-niramaya-app).

## Architecture

```
Cloud Scheduler (5:30 AM IST)
      │
      ▼
Cloud Run Job ──► generate.py
                      │
               ┌──────┴───────┐
               ▼              ▼
          builder.py     uploader.py
          (rotate +       (push to GCS)
           assemble)
               │
               ▼
    gs://samavaya-niramaya/daily/
        YYYY-MM-DD.json   ← PWA fetches dated
        latest.json       ← PWA fallback
```

## Files

| File | Purpose |
|------|---------|
| `generate.py` | Pipeline entrypoint (builder → uploader) |
| `builder.py` | Assembles daily JSON, rotates condition & wisdom by day-of-year |
| `uploader.py` | Uploads to GCS with public read & CORS |
| `config/content.yaml` | Content rotation lists (conditions, headlines) |
| `config/schedule.yaml` | Class schedule injected into every daily payload |
| `Dockerfile` | Cloud Run Job container |
| `deploy.sh` | One-shot GCS + Cloud Run + Scheduler setup |

## Local dev

```bash
pip install -r requirements.txt

# Build only (no GCS)
python generate.py 
  --source-json ../samavaya-niramaya-app/sample-data/2026-07-30.json 
  --skip-upload

# Output: /tmp/samavaya-daily/YYYY-MM-DD.json + latest.json
```

## Rotation logic

`builder.py` selects content deterministically by `day_of_year % len(list)`:

- **Featured condition** — cycles through 14 health conditions (e.g. day 1 → `lower_back_pain`, day 15 → `lower_back_pain` again)
- **Wisdom source** — cycles through 4 texts (Yoga Sutras, Bhagavad Gita, Upanishads, HYP)
- **Market headline** — cycles through 8 curated headlines

## Deployment

```bash
# Set your GCP project
gcloud config set project YOUR_PROJECT_ID

# Run once to create all infrastructure
bash deploy.sh

# Manually trigger a test run
gcloud run jobs execute samavaya-niramaya-pipeline --region=asia-south1
```

The scheduler fires at **00:00 UTC = 05:30 IST** daily.
