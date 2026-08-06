#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# deploy.sh — Samavaya Niramaya Pipeline Infrastructure Setup
# ═══════════════════════════════════════════════════════════
set -euo pipefail

PROJECT="${GCLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${GCLOUD_REGION:-asia-south1}"
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

# ── 1. Enable APIs ────────────────────────────────────────
echo "[1/8] Enabling APIs…"
gcloud services enable storage.googleapis.com run.googleapis.com cloudscheduler.googleapis.com artifactregistry.googleapis.com youtube.googleapis.com --project="${PROJECT}" --quiet

# ── 2. GCS bucket + CORS ──────────────────────────────────
echo "[2/8] Creating GCS bucket gs://${BUCKET}…"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" &>/dev/null; then
  gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" --project="${PROJECT}"
else
  echo "  Bucket already exists, skipping."
fi

echo "[2/8] Setting CORS…"
cat > /tmp/sn_cors.json <<'CORS'
[{"origin":["*"],"method":["GET","HEAD"],"responseHeader":["Content-Type","Cache-Control"],"maxAgeSeconds":3600}]
CORS
gcloud storage buckets update "gs://${BUCKET}" --cors-file=/tmp/sn_cors.json
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="allUsers" --role="roles/storage.objectViewer"

# ── 3. Artifact Registry ──────────────────────────────────
echo "[3/8] Creating Artifact Registry repo ${REPO}…"
if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}" --project="${PROJECT}" --quiet
else
  echo "  Repo already exists, skipping."
fi

# ── 4. Service account ────────────────────────────────────
echo "[4/8] Creating service account…"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" &>/dev/null; then
  gcloud iam service-accounts create samavaya-pipeline --display-name="Samavaya Niramaya Pipeline SA" --project="${PROJECT}"
fi
gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin" --quiet

# ── 5. Build & push Docker image ──────────────────────────
echo "[5/8] Building & pushing Docker image…"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform linux/amd64 -t "${IMAGE}" .
docker push "${IMAGE}"

# ── 6. Cloud Run Job ──────────────────────────────────────
echo "[6/8] Creating/updating Cloud Run Job ${JOB_NAME}…"
if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud run jobs update "${JOB_NAME}" --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" --service-account="${SA_EMAIL}" --max-retries=2 --set-env-vars="YOUTUBE_API_KEY=${YOUTUBE_API_KEY:-},GH_TOKEN=${GH_TOKEN:-}" --quiet
else
  gcloud run jobs create "${JOB_NAME}" --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" --service-account="${SA_EMAIL}" --max-retries=2 --set-env-vars="YOUTUBE_API_KEY=${YOUTUBE_API_KEY:-},GH_TOKEN=${GH_TOKEN:-}" --quiet
fi

# ── 7. IAM for Scheduler → Cloud Run ─────────────────────
echo "[7/8] Granting Cloud Scheduler invoke permission…"
gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${SA_EMAIL}" --role="roles/run.invoker" --quiet

# ── 8. Cloud Scheduler (5:30 AM IST = 00:00 UTC) ─────────
echo "[8/8] Creating Cloud Scheduler job…"
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" --location="${REGION}" --schedule="0 0 * * *" --uri="${JOB_URI}" --http-method=POST --oauth-service-account-email="${SA_EMAIL}" --project="${PROJECT}" --quiet
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" --location="${REGION}" --schedule="0 0 * * *" --uri="${JOB_URI}" --http-method=POST --oauth-service-account-email="${SA_EMAIL}" --project="${PROJECT}" --time-zone="UTC" --description="Samavaya Niramaya daily JSON — 5:30 AM IST" --quiet
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo "  GCS bucket  : gs://${BUCKET}/daily/"
echo "  Cloud Run   : ${JOB_NAME} (${REGION})"
echo "  Scheduler   : daily 00:00 UTC = 5:30 AM IST"
echo "  Image       : ${IMAGE}"
echo ""
echo "  Test run:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION}"
echo "════════════════════════════════════════════════"
