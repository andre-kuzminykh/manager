# Apps Script — Calendar proxy deployment

> FR-CR-05-136 — Web App that the meeting pipeline calls to
> read Google Calendar without a service account. Required
> when the operator's Google account doesn't have a billable
> GCP project (e.g. personal Google or shared workspace where
> service accounts aren't available).

## What this gives you

A single HTTPS endpoint, e.g.
`https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec`,
that the Python pipeline GETs with a from/to ISO range and a
shared-token guard. Responds with the operator's calendar
events for that window.

The Python side then runs an LLM-pass to pick the best match
and rewrites the meeting title to `DD/MM - <event title>`.

## One-time setup

1. **Open Apps Script** at <https://script.google.com/> as the
   Google account whose Calendar you want to read (typically
   the operator's primary).

2. **New project** → name it e.g. `manager-calendar-proxy`.

3. **Paste source**: copy
   [`docs/apps_script/calendar_proxy.gs`](./apps_script/calendar_proxy.gs)
   into the default `Code.gs` file. Save.

4. **Set the shared token**:
   - Edit the `setSharedToken()` function: replace `'CHANGE_ME'`
     with a strong random string, e.g. the output of
     `openssl rand -hex 32`. Note this value — you'll put it
     into the bot's env.
   - Click **Run** → `setSharedToken`. Approve the OAuth scope
     prompt (`Script Properties`).
   - Once complete, you can clear the literal value back to
     `'CHANGE_ME'` since the token is now persisted in the
     project's Script Properties.

5. **Authorise Calendar access**:
   - Click **Run** → `doGet`. Accept the OAuth prompt
     («This app isn't verified» — click *Advanced* → *Go to
     manager-calendar-proxy (unsafe)* → *Allow*). The script
     needs `CalendarApp` read permission.

6. **Deploy as Web App**:
   - Click **Deploy** (top right) → **New deployment**.
   - Type icon (gear) → **Web app**.
   - Description: `manager calendar proxy v1`.
   - Execute as: **Me** (the operator's account).
   - Who has access: **Anyone with the link** (the shared-token
     guard prevents unauthorised use; without "Anyone" the
     bot's HTTPS GET would 401).
   - Click **Deploy**.
   - Copy the **Web app URL** (looks like
     `https://script.google.com/macros/s/AKfyc.../exec`).

7. **Wire into the bot**:
   - Add to `~/manager/.env` (and any container env-file you
     use, e.g. `/tmp/tg-listener.env`):
     ```
     CALENDAR_MATCH_ENABLED=true
     CALENDAR_MATCH_WINDOW_MINUTES=30
     CALENDAR_APPS_SCRIPT_URL=https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec
     CALENDAR_APPS_SCRIPT_SHARED_TOKEN=<the random string from step 4>
     ```
   - Restart the listener container so the new env applies:
     ```bash
     sudo docker restart slack-task-tg-listener
     ```

## Verifying

```bash
# Smoke-test the proxy directly
TOKEN='<your shared token>'
URL='https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec'

curl -sG "$URL" \
  --data-urlencode "token=$TOKEN" \
  --data-urlencode "from=2026-04-30T08:00:00Z" \
  --data-urlencode "to=2026-04-30T11:00:00Z" \
  | jq .
```

Expected: `{ "events": [ {title: "...", start: "...", attendees: [...]}, ... ] }`.

After the next Fireflies pipeline run, watch the trace:

```bash
TRACE=$(ls -t ~/manager/traces/fireflies-*.jsonl | head -1)
jq -r 'select(.event=="calendar_match_events_fetched" or .event=="calendar_match_llm_picked" or .event=="calendar_match_title_updated") | "\(.event): \(.fields)"' "$TRACE"
```

Expected events in order:
- `calendar_match_events_fetched` — count + sample of candidate titles
- `calendar_match_llm_picked` — the title the LLM chose
- `calendar_match_title_updated` — old → new title in DB

## Updating the script later

If you change `calendar_proxy.gs`:
1. Save in Apps Script editor.
2. Deploy → **Manage deployments** → edit the existing
   deployment → bump version → **Deploy**.
3. The Web App URL stays the same — no need to update the env
   var.

## Rolling back

To disable without removing infra:
```
CALENDAR_MATCH_ENABLED=false
```
Restart listener. Pipeline silently skips the title-match step.

To revoke entirely: in Apps Script → **Manage deployments** →
**Archive**. Web App URL stops responding immediately.
