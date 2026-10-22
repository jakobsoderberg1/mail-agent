#!/usr/bin/env bash
# Deploys mail-agent to Cloud Run Jobs + Cloud Scheduler. Safe to re-run.
# Prereqs: gcloud logged in, .env filled in, token.json from `uv run main.py auth`.
#   PROJECT_ID=my-project ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-europe-west1}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-eur3}"
TIME_ZONE="Europe/Stockholm"
TRIAGE_SCHEDULE="*/15 * * * *"
DEBRIEF_SCHEDULE="0 7 * * 1-5"  # keep in sync with DEBRIEF_HOUR in agent.py
SA="mail-agent@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com gmail.googleapis.com calendar-json.googleapis.com

echo "==> Firestore"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 ||
  gcloud firestore databases create --location="$FIRESTORE_LOCATION"

echo "==> Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create mail-agent --display-name="mail-agent"
for role in roles/datastore.user roles/secretmanager.secretAccessor roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" \
    --role="$role" --condition=None >/dev/null
done

echo "==> Secrets"
env_value() { grep "^$1=" .env | cut -d= -f2-; }
put_secret() {
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    gcloud secrets versions add "$1" --data-file=-
  else
    gcloud secrets create "$1" --replication-policy=automatic --data-file=-
  fi
}
env_value ANTHROPIC_API_KEY | tr -d '\n' | put_secret anthropic-api-key
env_value TYPESAFE_API_KEY | tr -d '\n' | put_secret typesafe-api-key
env_value TELEGRAM_BOT_TOKEN | tr -d '\n' | put_secret telegram-bot-token
env_value TELEGRAM_CHAT_ID | tr -d '\n' | put_secret telegram-chat-id
put_secret google-token-json < token.json

SECRETS="ANTHROPIC_API_KEY=anthropic-api-key:latest,TYPESAFE_API_KEY=typesafe-api-key:latest"
SECRETS+=",TELEGRAM_BOT_TOKEN=telegram-bot-token:latest"
SECRETS+=",TELEGRAM_CHAT_ID=telegram-chat-id:latest,GOOGLE_TOKEN_JSON=google-token-json:latest"
COMMON=(--region="$REGION" --service-account="$SA" --set-secrets="$SECRETS"
  --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT_ID" --task-timeout=600 --max-retries=0)

echo "==> Building and deploying jobs"
gcloud run jobs deploy mail-agent-triage --source=. --args=triage "${COMMON[@]}"
IMAGE=$(gcloud run jobs describe mail-agent-triage --region="$REGION" \
  --format='value(spec.template.spec.template.spec.containers[0].image)')
gcloud run jobs deploy mail-agent-debrief --image="$IMAGE" --args=debrief "${COMMON[@]}"

echo "==> Schedules"
schedule() {
  local name=$1 cron=$2
  local verb=create
  gcloud scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1 && verb=update
  gcloud scheduler jobs "$verb" http "$name" --location="$REGION" \
    --schedule="$cron" --time-zone="$TIME_ZONE" --http-method=POST \
    --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/$name:run" \
    --oauth-service-account-email="$SA"
}
schedule mail-agent-triage "$TRIAGE_SCHEDULE"
schedule mail-agent-debrief "$DEBRIEF_SCHEDULE"

echo "Done. Test now with: gcloud run jobs execute mail-agent-triage --region=$REGION --wait"
