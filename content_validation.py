"""
Looker Content Validation Alerting Script.

Executes Looker's Content Validator via the Looker SDK, identifies broken Looks 
and Dashboards, groups them by how recently they were last viewed, and posts a 
formatted summary to a Slack channel using an Incoming Webhook.

State Management & Deduplication:
To prevent continuous alerting for the same broken content, previously reported 
issues are tracked by their IDs in `results/known_issues.json`. New breakages 
are added to this file only AFTER a successful Slack webhook POST (using an atomic 
write to prevent corruption). If the POST fails or the webhook URL is missing, 
the script exits without updating the state file, ensuring the alerts are retried 
on the next run.

Important CI/CD Note:
The state file (`results/known_issues.json`) must be persisted between runs 
(e.g., via git commits back to the repo, CI caching, or persistent storage). 
If the file is lost, the script will treat all existing broken content as "new" 
and re-alert.

Environment Variables Required:
- LOOKERSDK_BASE_URL: The URL of your Looker instance.
- LOOKERSDK_CLIENT_ID: Looker API client ID.
- LOOKERSDK_CLIENT_SECRET: Looker API client secret.
- WEBHOOK_URL: (Optional) Slack Incoming Webhook URL. If omitted, the script 
  writes the report locally but skips Slack alerting and state updates.
"""

import looker_sdk
import os
import sys
import json
import tempfile
import requests
from datetime import datetime, timezone
from collections import defaultdict

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

MAX_PAYLOAD_CHARS = 35000
KNOWN_ISSUES_FILE = os.path.join("results", "known_issues.json")

def get_time_bucket(last_viewed_at, now):
    """Categorizes a datetime into a specific time bucket."""
    if not last_viewed_at:
        return "older than 1 month / never viewed"

    if last_viewed_at.tzinfo is None:
        last_viewed_at = last_viewed_at.replace(tzinfo=timezone.utc)

    days_ago = (now - last_viewed_at).days
    if days_ago <= 7:
        return "last 1 week"
    elif days_ago <= 14:
        return "last 2 weeks"
    elif days_ago <= 30:
        return "last 1 month"
    else:
        return "older than 1 month / never viewed"

def save_known_issues(broken_ids):
    """
    Write state atomically — temp file in the same directory, then rename, so
    an interrupted run can't leave truncated JSON behind.
    """
    os.makedirs("results", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir="results", prefix=".known_issues.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(broken_ids), f)
        os.replace(tmp_path, KNOWN_ISSUES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    print(f"State file updated successfully ({len(broken_ids)} item(s) tracked).")

def monitor_and_group_errors():
    if load_dotenv:
        load_dotenv()

    sdk = looker_sdk.init40()
    print("Fetching usage data for Dashboards and Looks...")

    # Use search_* (not all_dashboards, which returns a lightweight
    # DashboardBase without last_viewed_at). No spaces in `fields` — a stray
    # space can cause the field to be silently dropped.
    dashboards = sdk.search_dashboards(fields="id,last_viewed_at")
    looks = sdk.search_looks(fields="id,last_viewed_at")

    # Prefix IDs to avoid collisions between Dashboard ID 1 and Look ID 1
    usage_map = {f"dash_{d.id}": d.last_viewed_at for d in dashboards}
    usage_map.update({f"look_{l.id}": l.last_viewed_at for l in looks})

    # ContentValidationFolder only exposes id + name, so fetch folders once to
    # know which are personal (personal or a descendant of one). `name` is a
    # required field on FolderBase, so it must be requested too.
    folders = sdk.all_folders(fields="id,name,is_personal,is_personal_descendant")
    personal_folders = {
        f.id: bool(f.is_personal or f.is_personal_descendant) for f in folders
    }

    # LOAD STATE
    known_issues = set()
    if os.path.exists(KNOWN_ISSUES_FILE):
        try:
            with open(KNOWN_ISSUES_FILE, "r", encoding="utf-8") as f:
                known_issues = set(json.load(f))
            print(f"Loaded {len(known_issues)} previously known broken item(s).")
        except Exception as e:
            print(f"Warning: could not load known issues: {e}", file=sys.stderr)
    else:
        # If this shows up on every run, the state file isn't being persisted
        # and dedup is doing nothing.
        print(
            f"Warning: no state file at {KNOWN_ISSUES_FILE} — treating all "
            f"current breakages as new.",
            file=sys.stderr,
        )

    print("Running content validation...")
    results = sdk.content_validation()

    grouped_errors = defaultdict(list)
    now = datetime.now(timezone.utc)

    new_errors_count = 0
    skipped_items = 0
    current_broken_ids = set()

    for item in (results.content_with_errors or []):
        try:
            # 1. Extract context
            if item.dashboard:
                c_type = "Dashboard"
                c_id = f"dash_{item.dashboard.id}"
                title = item.dashboard.title
                folder = item.dashboard.folder
            elif item.look:
                c_type = "Look"
                c_id = f"look_{item.look.id}"
                title = item.look.title
                folder = item.look.folder
            else:
                continue

            # Check if we already alerted on this
            if c_id in known_issues:
                # Still broken, so keep it in our state file for next run
                current_broken_ids.add(c_id)
                continue

            # 2. Determine Folder Context
            if folder:
                if personal_folders.get(folder.id, False):
                    folder_label = f"👤 Personal ({folder.name})"
                else:
                    folder_label = f"📁 Shared ({folder.name})"
            else:
                folder_label = "Unknown Folder"

            # 3. Get Time Bucket
            last_viewed = usage_map.get(c_id)
            bucket = get_time_bucket(last_viewed, now)

            # 4. Format errors
            error_messages = [f"    ↳ *Error:* {err.message}" for err in item.errors]
            formatted_item = (
                f"• *[{c_type}]* {title} | `{folder_label}`\n" + "\n".join(error_messages)
            )

            grouped_errors[bucket].append(formatted_item)
            new_errors_count += 1

            # Add to state ONLY if it successfully formatted without throwing an exception
            current_broken_ids.add(c_id)

        except Exception as e:
            skipped_items += 1
            print(f"Warning: skipped one content_validation item due to: {e!r}", file=sys.stderr)
            continue

    # 5. Build Payload & Save TXT Report
    os.makedirs("results", exist_ok=True)
    result_path = os.path.join("results", f"results_{now.strftime('%Y%m%d_%H%M')}.txt")

    if new_errors_count > 0:
        bucket_order = [
            "last 1 week",
            "last 2 weeks",
            "last 1 month",
            "older than 1 month / never viewed",
        ]
        bucket_labels = {
            "last 1 week": "viewed in the last 1 week",
            "last 2 weeks": "viewed in the last 2 weeks",
            "last 1 month": "viewed in the last 1 month",
            "older than 1 month / never viewed": "viewed over 1 month ago or never viewed",
        }

        final_alert_lines = [
            "🚨 *Looker Content Validation Report*",
            f"_{new_errors_count} *new* broken items found._\n",
        ]

        if skipped_items:
            final_alert_lines.append(
                f"_⚠️ {skipped_items} item(s) skipped due to an unexpected API response shape._\n"
            )

        for bucket in bucket_order:
            items_in_bucket = grouped_errors.get(bucket, [])
            if not items_in_bucket:
                continue

            count = len(items_in_bucket)
            final_alert_lines.append(f"*{count} item(s) {bucket_labels[bucket]}:*")

            for formatted_item in items_in_bucket:
                final_alert_lines.append(formatted_item)

            final_alert_lines.append("\n")

        payload_text = "\n".join(final_alert_lines)

        if len(payload_text) > MAX_PAYLOAD_CHARS:
            truncated = payload_text[:MAX_PAYLOAD_CHARS].rsplit("\n", 1)[0]
            payload_text = (
                truncated
                + "\n\n_...truncated — too many broken items for one Slack message._"
            )

        # Write txt report
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(payload_text)

        # 6. Send to Slack
        webhook_url = os.environ.get("WEBHOOK_URL")
        if webhook_url:
            try:
                response = requests.post(webhook_url, json={"text": payload_text}, timeout=10)
                response.raise_for_status()
                print("Alert sent to webhook.")
            except requests.RequestException as e:
                print(f"Failed to send Slack alert: {e}", file=sys.stderr)
                # Raise kills the script BEFORE state is updated.
                # Next run will re-detect these items as "new" and retry alerting.
                raise
        else:
            # Nothing was delivered, so don't record these as alerted-on —
            # otherwise a missing or renamed secret silently eats every alert.
            print(
                f"WEBHOOK_URL is not set — skipping Slack alert and leaving state "
                f"unchanged so these are retried. Report available at {result_path}.",
                file=sys.stderr,
            )
            return

    else:
        # NO NOISE: Write report for CI commit, but do not send a Slack message.
        total_broken = len(current_broken_ids)
        payload_text = f"✅ Looker Content Validation: No new issues found. (There are {total_broken} previously known broken items)."

        with open(result_path, "w", encoding="utf-8") as f:
            f.write(payload_text)
        print(payload_text + " Skipping Slack alert to reduce channel noise.")

    # 7. SAVE STATE
    # Only reached if the Slack POST succeeded or there were zero new errors.
    # A failed POST raises above; an unconfigured webhook returns above.
    save_known_issues(current_broken_ids)

if __name__ == "__main__":
    monitor_and_group_errors()
