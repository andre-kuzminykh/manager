# Run & Deploy

This guide has two halves:

1. **Local verification** — run everything on your machine with
   `docker compose` and check each flow in Slack.
2. **GCP deployment** — a minimal production path using Compute Engine +
   Cloud SQL + Artifact Registry + Secret Manager. An optional Cloud Run
   variant is included at the end.

The bot uses **Socket Mode**, so you do not need any public HTTP endpoint.
Slack reaches the bot over an outbound WebSocket.

---

## 0. One-time prerequisites

### 0.1 Slack app

1. Go to <https://api.slack.com/apps> → **Create New App → From an app
   manifest**.
2. Paste `ops/slack-manifest.yaml`. Create.
3. **Basic Information** → **App-Level Tokens** → *Generate*. Scope:
   `connections:write`. Copy the token → this is `SLACK_APP_TOKEN`
   (starts with `xapp-`).
4. **Install App** into your workspace. Copy the *Bot User OAuth Token*
   → this is `SLACK_BOT_TOKEN` (starts with `xoxb-`).
5. Invite the bot into the target channels/MPIMs:
   `/invite @Task Manager Bot`.

### 0.2 Google Cloud project + APIs

```bash
export PROJECT_ID=your-gcp-project
export REGION=europe-west1         # pick yours
gcloud config set project "$PROJECT_ID"

# Enable APIs
gcloud services enable \
  compute.googleapis.com \
  sqladmin.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  sheets.googleapis.com \
  tasks.googleapis.com
```

### 0.3 Google OAuth client

1. In Google Cloud Console → **APIs & Services → Credentials** create an
   **OAuth 2.0 Client ID** of type *Desktop app*.
2. Copy the client id + secret into `.env`
   (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).
3. Create / pick the Google Sheet you want tasks written to; grab its id
   from the URL → `GOOGLE_SHEETS_SPREADSHEET_ID`.
4. (Optional) Create a dedicated Google Tasks list; id → `GOOGLE_TASKS_DEFAULT_TASKLIST_ID`.
   Leaving the default `@default` also works.

### 0.4 Generate encryption key

```bash
python ops/generate_fernet_key.py
```

Put the output into `SECRETS_ENCRYPTION_KEY` (env var / Secret Manager).
This key encrypts OAuth tokens in Postgres. **Do not lose it.**

### 0.5 Anthropic API key (optional but recommended)

Set `ANTHROPIC_API_KEY`. Without it the bot falls back to the rule
prefilter only (works, but low-quality extraction).

---

## 1. Local verification with docker compose

### 1.1 Fill `.env`

```bash
cp .env.example .env
# edit .env and set:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
#   DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/slack_tasks
#   SECRETS_ENCRYPTION_KEY=<output of generate_fernet_key.py>
#   ANTHROPIC_API_KEY=sk-ant-...
#   GOOGLE_CLIENT_ID=...
#   GOOGLE_CLIENT_SECRET=...
#   GOOGLE_SHEETS_SPREADSHEET_ID=...
```

### 1.2 Bring it up

```bash
docker compose up --build
```

This starts three services:

- `db` — Postgres 16
- `migrate` — one-shot `alembic upgrade head`
- `bot` — Socket Mode process

You should see logs like `starting_socket_mode` and, from Bolt,
`⚡️ Bolt app is running!`.

### 1.3 Seed Google credentials (once per environment)

In a second terminal, while the stack is up:

```bash
# Run the OAuth flow inside the bot container so it uses the same DB.
docker compose exec bot python -m ops.bootstrap_google_oauth
```

A browser window opens. Grant access with your Google account. The
encrypted refresh token is stored in `oauth_credentials`. From now on
the bot can write to your Sheet and your Google Tasks list.

> If running `bootstrap_google_oauth` inside Docker is awkward (no
> browser), run it on your host instead with the same `.env` but
> `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/slack_tasks`.

### 1.4 Smoke test each flow

| Flow | What to do | Expected |
| ---- | ---------- | -------- |
| **UC-1.1 passive detection** | In a channel/DM where the bot is a member, post: `Надо подготовить список фондов до пятницы.` | Bot posts a **draft card** in the thread with title, owner, due date and three buttons. |
| **UC-1.2 ambiguous** | Post: `Что-то сегодня я устал.` | Bot stays silent (no `no_action` notifications). |
| **Soft prompt** | Post something borderline like `может надо созвон на следующей неделе` | Medium-confidence soft prompt with Yes / No. |
| **UC-2.1 mention** | `@Task Manager Bot создай задачу: собрать отчёт по фандрейзингу до понедельника` | Bot extracts and posts a confirmation card. |
| **UC-2.2 shortcut** | Hover a message → *More actions* (⋮) → **Create task from message** | Modal opens with title prefilled from the message. Submit creates a task. |
| **Edit** | Click *Edit* on a draft card | Modal opens pre-populated with current payload; submitting updates the draft and finalizes. |
| **Confirm → persist → sync** | Click *Confirm* on a task draft | Success message in Slack, new row in the Google Sheet, new item in Google Tasks, DB row in `tasks`. |
| **Dedup** | Post a message, then reconnect the container (`docker compose restart bot`). Slack may re-deliver. | Only one draft card is shown. |
| **Rate limit** | Confirm several cards in quick succession | No 429s bubble up; sender throttles to 1 msg/s per channel. |

### 1.5 Inspect DB

```bash
docker compose exec db psql -U postgres -d slack_tasks -c "select id,title,due_date,source_conversation_id,source_message_ts,google_sheets_row_id,google_tasks_id from tasks order by id desc limit 10;"
docker compose exec db psql -U postgres -d slack_tasks -c "select id,intent,state,inference_id from action_drafts order by id desc limit 10;"
docker compose exec db psql -U postgres -d slack_tasks -c "select task_id,status,attempts,last_error from google_sheets_sync order by id desc limit 5;"
```

Each confirmed task must have `source_message_ts` + `source_conversation_id`
populated (the traceability invariant from the SPEC).

### 1.6 Run tests

```bash
docker compose run --rm bot pytest -q
# or locally:
pip install -e .[dev] && pytest -q
```

24 tests, all in-memory — no Slack or Google calls.

---

## 2. Production on GCP

The simplest Socket Mode-compatible setup is a single GCE VM with the
container, pointed at Cloud SQL. Scale to GKE later if needed.

### 2.1 Create Cloud SQL (Postgres 16)

```bash
gcloud sql instances create slack-tasks \
  --database-version=POSTGRES_16 \
  --tier=db-f1-micro \
  --region="$REGION" \
  --storage-size=10GB \
  --storage-auto-increase

gcloud sql databases create slack_tasks --instance=slack-tasks
gcloud sql users create bot --instance=slack-tasks --password="$(openssl rand -hex 16 | tee /tmp/dbpw)"

DB_HOST=$(gcloud sql instances describe slack-tasks --format='value(ipAddresses[0].ipAddress)')
DB_PW=$(cat /tmp/dbpw)
DB_URL="postgresql+psycopg://bot:${DB_PW}@${DB_HOST}:5432/slack_tasks"
```

(In a real environment put this behind Private IP + VPC peering and use
the Cloud SQL Auth Proxy instead of a public IP.)

### 2.2 Store secrets in Secret Manager

```bash
for KEY in SLACK_BOT_TOKEN SLACK_APP_TOKEN ANTHROPIC_API_KEY \
           GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET \
           GOOGLE_SHEETS_SPREADSHEET_ID GOOGLE_TASKS_DEFAULT_TASKLIST_ID \
           SECRETS_ENCRYPTION_KEY DATABASE_URL; do
  echo "Enter value for $KEY:"; read -r VALUE
  printf '%s' "$VALUE" | gcloud secrets create "$KEY" --data-file=- 2>/dev/null \
    || printf '%s' "$VALUE" | gcloud secrets versions add "$KEY" --data-file=-
done
```

### 2.3 Build and push the image

```bash
gcloud artifacts repositories create bots --repository-format=docker --location="$REGION"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/bots/slack-task-bot:$(git rev-parse --short HEAD)"
gcloud builds submit --tag "$IMAGE"
```

### 2.4 Run migrations once

From any machine that can reach Cloud SQL (or via Cloud SQL Auth Proxy):

```bash
docker run --rm \
  -e DATABASE_URL="$DB_URL" \
  "$IMAGE" \
  alembic upgrade head
```

### 2.5 Seed Google credentials once

The installed-app OAuth flow needs a browser, so run it on your laptop
pointed at the production DB (via Cloud SQL Auth Proxy):

```bash
cloud_sql_proxy -instances="$PROJECT_ID:$REGION:slack-tasks=tcp:5432" &
DATABASE_URL="postgresql+psycopg://bot:${DB_PW}@127.0.0.1:5432/slack_tasks" \
SECRETS_ENCRYPTION_KEY="$(gcloud secrets versions access latest --secret=SECRETS_ENCRYPTION_KEY)" \
GOOGLE_CLIENT_ID="$(gcloud secrets versions access latest --secret=GOOGLE_CLIENT_ID)" \
GOOGLE_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=GOOGLE_CLIENT_SECRET)" \
python -m ops.bootstrap_google_oauth
```

### 2.6 Deploy to a GCE VM with Container-Optimized OS

```bash
gcloud compute instances create-with-container slack-task-bot \
  --zone="${REGION}-b" \
  --machine-type=e2-micro \
  --container-image="$IMAGE" \
  --container-restart-policy=always \
  --container-env=DATABASE_URL="$DB_URL" \
  --container-env-file=<(for k in SLACK_BOT_TOKEN SLACK_APP_TOKEN ANTHROPIC_API_KEY \
      GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_SHEETS_SPREADSHEET_ID \
      GOOGLE_TASKS_DEFAULT_TASKLIST_ID SECRETS_ENCRYPTION_KEY; do
        printf '%s=%s\n' "$k" "$(gcloud secrets versions access latest --secret="$k")"
      done) \
  --tags=slack-bot \
  --scopes=cloud-platform
```

Verify it is alive:

```bash
gcloud compute ssh slack-task-bot --zone="${REGION}-b" --command \
  "docker ps && docker logs \$(docker ps -q) --tail=50"
```

You should see `⚡️ Bolt app is running!` and `starting_socket_mode`.

### 2.7 Verify in Slack

Repeat the smoke tests from §1.4. The DB inspection queries work the same
from Cloud SQL Auth Proxy.

---

## 3. Cloud Run variant (optional)

Cloud Run services expect the container to listen on `$PORT`, which a
pure Socket Mode process does not do. Use the alternate entrypoint that
runs the bot in a thread behind a tiny HTTP health endpoint:

```dockerfile
# Dockerfile.cloudrun
FROM <your image>
CMD ["python", "-m", "ops.entrypoint_with_health"]
```

Deploy with `min-instances=1` and CPU always allocated so the WebSocket
stays connected when no HTTP traffic arrives:

```bash
gcloud run deploy slack-task-bot \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --min-instances=1 \
  --max-instances=1 \
  --no-cpu-throttling \
  --concurrency=1 \
  --no-allow-unauthenticated \
  --command=python --args="-m,ops.entrypoint_with_health" \
  --set-secrets=SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,SLACK_APP_TOKEN=SLACK_APP_TOKEN:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GOOGLE_CLIENT_ID=GOOGLE_CLIENT_ID:latest,GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET:latest,GOOGLE_SHEETS_SPREADSHEET_ID=GOOGLE_SHEETS_SPREADSHEET_ID:latest,GOOGLE_TASKS_DEFAULT_TASKLIST_ID=GOOGLE_TASKS_DEFAULT_TASKLIST_ID:latest,SECRETS_ENCRYPTION_KEY=SECRETS_ENCRYPTION_KEY:latest,DATABASE_URL=DATABASE_URL:latest \
  --add-cloudsql-instances="$PROJECT_ID:$REGION:slack-tasks"
```

Notes:
- `--max-instances=1` prevents multiple WebSocket connections.
- `--no-cpu-throttling` is required so the background thread can keep
  the socket alive between requests.
- Without traffic Cloud Run still bills because of `min-instances=1`
  with CPU always allocated. For a truly idle deploy, prefer GCE.

---

## 4. Troubleshooting

| Symptom | Cause / Fix |
| ------- | ----------- |
| Bot ignores channel messages but responds to DMs | Make sure the bot was invited (`/invite @bot`) and `channels:history` / `groups:history` scopes are granted. |
| Shortcuts don't appear | Reinstall the app after adding shortcuts to the manifest. |
| `missing_slack_app_token_for_socket_mode` on start | `SLACK_APP_TOKEN` is unset. Generate at **Basic Information → App-Level Tokens** with `connections:write`. |
| `RuntimeError: SECRETS_ENCRYPTION_KEY is not set` when syncing | Set the key (§0.4) and re-run `ops.bootstrap_google_oauth`. |
| Tasks are not appearing in Sheets | Check `google_sheets_sync.last_error`; most common issue is the service account hasn't been granted editor access to the Sheet. |
| `HttpError 403` from Google Tasks | Run `ops.bootstrap_google_oauth` again with the correct Google account. Scopes must include `https://www.googleapis.com/auth/tasks`. |
| Duplicate draft cards | Check Slack `Request URL` retries; the `processed_slack_events` table dedups on `event_id`. Ensure your DB is persistent. |
