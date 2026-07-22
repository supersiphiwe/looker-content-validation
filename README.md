# Looker Content Validation → Slack Alerts

Runs Looker's Content Validator on a schedule and posts a summary of any
broken Looks/Dashboards to a Slack channel, grouped by how recently each
item was last viewed.

## Repo secrets required

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Where to get it |
|---|---|
| `LOOKERSDK_BASE_URL` | Your Looker instance's API host, e.g. `https://your-instance.looker.com:19999` — check Admin → API on your instance for the exact host/port, since this varies by hosting type |
| `LOOKERSDK_CLIENT_ID` | Admin → Users → your user or service account → API3 Keys |
| `LOOKERSDK_CLIENT_SECRET` | Same API3 Keys screen (only shown once, at creation) |
| `SLACK_WEBHOOK_URL` | A Slack app with the Incoming Webhooks feature enabled, pointed at your target channel |

`looker_sdk.init40()` reads the three `LOOKERSDK_*` values straight from
the environment, so no `looker.ini` file needs to live in this repo.

> **Note:** the script itself reads the webhook from an env var named
> `WEBHOOK_URL`. The workflow maps the `SLACK_WEBHOOK_URL` repo secret to
> `WEBHOOK_URL` at run time, so you only ever set the secret once under the
> name above.

## Schedule

Set in `.github/workflows/looker-content-validation.yaml`:

```yaml
on:
  schedule:
    - cron: '0 7 * * 1-5'   # 7am UTC, weekdays
```

Adjust the cron expression as needed. You can also trigger a run by hand
from the **Actions** tab — the workflow includes `workflow_dispatch`.

## Running locally

```bash
pip install -r requirements.txt

export LOOKERSDK_BASE_URL=https://your-instance.looker.com:19999
export LOOKERSDK_CLIENT_ID=...
export LOOKERSDK_CLIENT_SECRET=...
export WEBHOOK_URL=...   # omit this to print the report to stdout instead of posting to Slack

python content_validation.py
```

## If your Looker instance restricts API access by IP

GitHub-hosted runners use dynamic IPs, so they won't get through an IP
allowlist. If that applies to your instance, use a self-hosted runner, or
open that restriction for this use case.

## What the script guards against

- Broken content that's *never been viewed* is grouped correctly instead
  of silently disappearing from the report (see comments in
  `content_validation.py` for the original bug this fixes).
- A failed Slack post (bad/revoked webhook, archived channel) fails the
  Action run instead of reporting success with nothing delivered.
- One unexpectedly-shaped item from the Looker API is logged and skipped
  rather than crashing the whole run — the report will note how many
  items were skipped, if any.
- Very large reports are truncated with a pointer back to Looker, instead
  of risking rejection by Slack's message size limits.