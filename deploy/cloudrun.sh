#!/usr/bin/env bash
# Provision and deploy the API + approval screen to Google Cloud Run.
#
#   PROJECT_ID=your-project ./deploy/cloudrun.sh
#
# Idempotent: every step is guarded, so this is also the redeploy command. It prints the
# service URL at the end, which is what Razorpay's webhook endpoint and the showcase's
# "live app" link both need.
#
# ---------------------------------------------------------------------------------------
# Why Cloud Run, and why it is pinned to one instance
#
# The store is a SQLite file (adapters/storage/db.py argues that choice). SQLite takes one
# writer, so the deployment takes one instance: `--max-instances=1`. That is not a knob to
# turn up when traffic grows — going wider is a database migration, not a flag.
#
# Durability comes from Litestream replicating the file to a GCS bucket continuously, so the
# container filesystem stays disposable. `--no-cpu-throttling` is what makes that true: with
# Cloud Run's default throttling the process gets no CPU between requests, and a background
# replicator that never runs is not a backup. It pairs with `--min-instances=1`; the two
# together are the cost of this service (roughly $10-15/month) and the reason it is honest
# to call the data durable.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your Google Cloud project}"
REGION="${REGION:-asia-south1}"          # Mumbai — the payments this reconciles are INR.
SERVICE="${SERVICE:-precedent}"
BUCKET="${BUCKET:-${PROJECT_ID}-precedent-db}"
SA_NAME="${SA_NAME:-precedent-run}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
SEED_ON_EMPTY="${SEED_ON_EMPTY:-1}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null

say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com

say "Bucket for the replicated database"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  # Uniform access and versioning: versioning is the difference between "the file is
  # replicated" and "a bad write can be undone", which is the failure this project cares
  # about — a poisoned corpus is worse than a lost one.
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="$REGION" --uniform-bucket-level-access
  gcloud storage buckets update "gs://${BUCKET}" --versioning
else
  echo "gs://${BUCKET} already exists"
fi

say "Runtime service account"
if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Precedent Cloud Run runtime"
else
  echo "$SA_EMAIL already exists"
fi

# Scoped to the one bucket rather than project-wide storage access.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" >/dev/null

say "Secrets"
# Razorpay's three are required — webhooks/verify_webhook_signature cannot check a delivery
# without the webhook secret, and an unverified delivery is not evidence of anything.
#
# The model key is required too, and for a sharper reason: confirming a resolution authors a
# precedent, so the approval gate calls a model *in the request path*. Without a key the gate
# refuses every confirmation rather than banking a review with nothing in the corpus behind
# it — correct behaviour, and a dead demo. See `api/ui.py::decide`.
put_secret() {
  local name="$1" value="${2:-}"
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  if [ -n "$value" ]; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "  $name updated from the environment"
  elif ! gcloud secrets versions list "$name" --limit=1 --format='value(name)' \
        | grep -q .; then
    echo "  !! $name has no version yet — add one before the service will start:"
    echo "     printf '%s' 'THE_VALUE' | gcloud secrets versions add $name --data-file=-"
  else
    echo "  $name already has a version"
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

# Values are picked up from your shell if exported (e.g. `set -a; . ./.env; set +a`), and
# otherwise left to whatever is already stored — so re-running never blanks a live secret.
put_secret razorpay-key-id         "${RAZORPAY_KEY_ID:-}"
put_secret razorpay-key-secret     "${RAZORPAY_KEY_SECRET:-}"
put_secret razorpay-webhook-secret "${RAZORPAY_WEBHOOK_SECRET:-}"
put_secret nvidia-api-key           "${NVIDIA_API_KEY:-}"

say "Building and deploying"
gcloud run deploy "$SERVICE" \
  --source=. \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --execution-environment=gen2 \
  --cpu=1 --memory=512Mi \
  --max-instances=1 \
  --min-instances="$MIN_INSTANCES" \
  --no-cpu-throttling \
  --timeout=120s \
  --allow-unauthenticated \
  --set-env-vars="PRECEDENT_DB_PATH=/data/precedent.db,LITESTREAM_REPLICA_URL=gcs://${BUCKET}/precedent,PRECEDENT_SEED_ON_EMPTY=${SEED_ON_EMPTY}" \
  --set-secrets="RAZORPAY_KEY_ID=razorpay-key-id:latest,RAZORPAY_KEY_SECRET=razorpay-key-secret:latest,RAZORPAY_WEBHOOK_SECRET=razorpay-webhook-secret:latest,NVIDIA_API_KEY=nvidia-api-key:latest"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"

say "Deployed"
cat <<EOF
  Approval screen   ${URL}/
  Health            ${URL}/healthz
  Webhook endpoint  ${URL}/webhooks/razorpay

Next:
  1. Razorpay Dashboard -> Settings -> Webhooks -> add ${URL}/webhooks/razorpay
     with the same secret stored in razorpay-webhook-secret, subscribed to
     payment.captured and refund.processed.
  2. Rebuild the showcase so it links here:
       PRECEDENT_APP_URL=${URL} python3 scripts/build_site.py && npx wrangler pages deploy
EOF
