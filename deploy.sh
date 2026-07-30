#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# deploy.sh — Samavaya Niramaya Pipeline Infrastructure Setup
# ═══════════════════════════════════════════════════════════
# Creates:
#   - GCS bucket  gs://samavaya-niramaya  (with CORS for PWA)
#   - Artifact Registry repo for Docker image
#   - Cloud Run Job (samavaya-niramaya-pipeline)
#   - Cloud Scheduler job (5:30 AM IST = 00:00 UTC)
#
# Prerequisites:
#   gcloud auth login && gcloud auth application-default login
#   gcloud config set project YOUR_PROJECT_ID
#
# Usage:
#   bash deploy.sh [--project PROJECT_ID] [--region REGION]
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ── Config (override via env or args) ────────────────────
PROJECT="${GCLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${GCLOUD_REGION:-asia-south1}"           # Mumbai — closest to Pune IST
BUCKET="samavaya-niramaya"
REPO="samavaya"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/pipeline:latest"
JOB_NAME="samavaya-niramaya-pipeline"
SCHEDULER_JOB="samavaya-niramaya-daily"
SA_EMAIL="samavaya-pipeline@${PROJECT}.iam.gserviceaccount.com"

echo ""
echo "════════════════════════════════════════════════"
echo "  🪷 Samavaya Niramaya — Infrastructure Deploy"
echo "  Project : ${PROJECT}"
echo "  Region  : ${REGION}"
echo "════════════════════════════════════════════════"
echo ""

# ── 1. Enable required APIs ───────────────────────────────
echo "[1/8] Enabling APIs…"
gcloud services enable 
  storage.googleapis.com 
  run.googleapis.com 
  cloudscheduler.googleapis.com 
  artifactregistry.googleapis.com 
  --project="${PROJECT}" --quiet

# ── 2. Create GCS bucket ──────────────────────────────────
echo "[2/8] Creating GCS bucket gs://${BUCKET}…"
if ! gsutil ls -b "gs://${BUCKET}" &>/dev/null; then
  gsutil mb -p "${PROJECT}" -l "${REGION}" "gs://${BUCKET}"
else
  echo "  Bucket already exists, skipping."
fi

# CORS — allow PWA fetch from any origin
echo "[2/8] Setting CORS on bucket…"
cat > /tmp/sn_cors.json <<'CORS'
[
  {
    "origin": ["*"],
    "method": ["GET", "HEAD"],
    "responseHeader": ["Content-Type", "Cache-Control"],
    "maxAgeSeconds": 3600
  }
]
CORS
gsutil cors set /tmp/sn_cors.json "gs://${BUCKET}"

# Public read access for daily/ prefix (PWA fetches unauthenticated)
gsutil iam ch allUsers:objectViewer "gs://${BUCKET}"

# ── 3. Create Artifact Registry repo ─────────────────────
echo "[3/8] Creating Artifact Registry repo ${REPO}…"
if ! gcloud artifacts repositories describe "${REPO}" 
    --location="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud artifacts repositories create "${REPO}" 
    --repository-format=docker 
    --location="${REGION}" 
    --project="${PROJECT}" 
    --quiet
else
  echo "  Repo already exists, skipping."
fi

# ── 4. Create service account ─────────────────────────────
echo "[4/8] Creating service account…"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" 
    --project="${PROJECT}" &>/dev/null; then
  gcloud iam service-accounts create samavaya-pipeline 
    --display-name="Samavaya Niramaya Pipeline SA" 
    --project="${PROJECT}"
fi

# Grant GCS write access
gcloud projects add-iam-policy-binding "${PROJECT}" 
  --member="serviceAccount:${SA_EMAIL}" 
  --role="roles/storage.objectAdmin" 
  --quiet

# ── 5. Build & push Docker image ──────────────────────────
echo "[5/8] Building & pushing Docker image…"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "${IMAGE}" .
docker push "${IMAGE}"

# ── 6. Create Cloud Run Job ───────────────────────────────
echo "[6/8] Creating Cloud Run Job ${JOB_NAME}…"
if gcloud run jobs describe "${JOB_NAME}" 
    --region="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud run jobs update "${JOB_NAME}" 
    --image="${IMAGE}" 
    --region="${REGION}" 
    --project="${PROJECT}" 
    --service-account="${SA_EMAIL}" 
    --max-retries=2 
    --quiet
else
  gcloud run jobs create "${JOB_NAME}" 
    --image="${IMAGE}" 
    --region="${REGION}" 
    --project="${PROJECT}" 
    --service-account="${SA_EMAIL}" 
    --max-retries=2 
    --quiet
fi

# ── 7. Grant Scheduler permission to invoke the job ───────
echo "[7/8] Granting Cloud Scheduler → Cloud Run Job invoke permission…"
gcloud projects add-iam-policy-binding "${PROJECT}" 
  --member="serviceAccount:${SA_EMAIL}" 
  --role="roles/run.invoker" 
  --quiet

# ── 8. Create Cloud Scheduler job (5:30 AM IST = 00:00 UTC) ──
echo "[8/8] Creating Cloud Scheduler job (cron: 0 0 * * * UTC = 5:30 AM IST)…"
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" 
    --location="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" 
    --location="${REGION}" 
    --schedule="0 0 * * *" 
    --uri="${JOB_URI}" 
    --http-method=POST 
    --oauth-service-account-email="${SA_EMAIL}" 
    --project="${PROJECT}" 
    --quiet
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" 
    --location="${REGION}" 
    --schedule="0 0 * * *" 
    --uri="${JOB_URI}" 
    --http-method=POST 
    --oauth-service-account-email="${SA_EMAIL}" 
    --project="${PROJECT}" 
    --time-zone="UTC" 
    --description="Samavaya Niramaya daily JSON pipeline — 5:30 AM IST" 
    --quiet
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo ""
echo "  GCS bucket  : gs://${BUCKET}/daily/"
echo "  Cloud Run   : ${JOB_NAME} (${REGION})"
echo "  Scheduler   : ${SCHEDULER_JOB} — 0 0 * * * UTC (5:30 AM IST)"
echo "  Image       : ${IMAGE}"
echo ""
echo "  To trigger a test run:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION}"
echo "════════════════════════════════════════════════"
