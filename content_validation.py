"""
Looker Content Validation -> Slack alert.

Runs Looker's Content Validator, groups any broken Looks/Dashboards by how
recently they were last viewed, and posts a summary to a Slack channel via
an Incoming Webhook.

Fixes applied vs. the original version:
  - get_time_bucket() no longer returns two differently-cased strings for
    "old" vs. "never viewed" content. Previously "Older than 1 month /
    Never viewed" (capitalized) and "older than 1 month / never viewed"
    (lowercase) were treated as different dict keys, so never-viewed
    content was silently dropped from the rendered report even though it
    was still counted in the total.
  - The Slack POST now sets a timeout and calls raise_for_status(), so a
    revoked webhook / archived channel / bad payload surfaces as a failed
    Action run instead of a silent "Alert sent to webhook." with nothing
    actually delivered.
  - A missing WEBHOOK_URL now prints a loud warning to stderr (still falls
    back to printing the report to stdout, for local testing).
  - Each item is now processed in its own try/except so one unexpectedly-
    shaped item can't take down the whole run; skipped items are counted
    and called out in the final report instead of vanishing.
  - A basic length guard on the final payload, in case a large validation
    run (e.g. right after a LookML refactor) produces more text than Slack
    will accept in one message.
"""

import looker_sdk
import os
import sys
import requests
from datetime import datetime, timezone
from collections import defaultdict

# Keep a safety margin under Slack's message size limits.
MAX_PAYLOAD_CHARS = 35000


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


def monitor_and_group_errors():
    sdk = looker_sdk.init40()

    print("Fetching usage data for Dashboards and Looks...")
    # Fetch usage for both types to build a unified lookup map
    dashboards = sdk.all_dashboards(fields="id, last_viewed_at")
    looks = sdk.all_looks(fields="id, last_viewed_at")

    # Prefix IDs to avoid collisions between Dashboard ID 1 and Look ID 1
    usage_map = {f"dash_{d.id}": d.last_viewed_at for d in dashboards}
    usage_map.update({f"look_{l.id}": l.last_viewed_at for l in looks})

    print("Running content validation...")
    results = sdk.content_validation()

    grouped_errors = defaultdict(list)
    now = datetime.now(timezone.utc)
    total_errors = 0
    skipped_items = 0

    for item in results.content_with_errors:
        try:
            # 1. Extract context based on whether it's a Dashboard or a Look
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
                continue  # Skip edge case system files

            # 2. Determine Folder Context
            if folder:
                if folder.is_personal:
                    folder_label = f"👤 Personal ({folder.name})"
                else:
                    folder_label = f"📁 Shared ({folder.name})"
            else:
                folder_label = "Unknown Folder"

            # 3. Get Time Bucket
            last_viewed = usage_map.get(c_id)
            bucket = get_time_bucket(last_viewed, now)

            # 4. Format the item and its errors
            error_messages = [f"    ↳ *Error:* {err.message}" for err in item.errors]

            formatted_item = f"• *[{c_type}]* {title} | `{folder_label}`\n" + "\n".join(error_messages)

            grouped_errors[bucket].append(formatted_item)
            total_errors += 1

        except Exception as e:
            # Don't let one unexpectedly-shaped item take down the whole alert.
            skipped_items += 1
            print(f"Warning: skipped one content_validation item due to: {e!r}", file=sys.stderr)
            continue

    # Define strict bucket rendering order
    bucket_order = [
        "last 1 week",
        "last 2 weeks",
        "last 1 month",
        "older than 1 month / never viewed",
    ]

    final_alert_lines = [
        "🚨 *Looker Content Validation Report*",
        f"_{total_errors} broken items found._\n",
    ]

    if skipped_items:
        final_alert_lines.append(
            f"_⚠️ {skipped_items} item(s) skipped due to an unexpected API response shape "
            f"— check the run logs._\n"
        )

    for bucket in bucket_order:
        items_in_bucket = grouped_errors.get(bucket, [])

        if not items_in_bucket:
            continue

        count = len(items_in_bucket)
        final_alert_lines.append(f"*{count} item(s) viewed in the {bucket}:*")

        for formatted_item in items_in_bucket:
            final_alert_lines.append(formatted_item)

        final_alert_lines.append("\n")  # Spacing between buckets

    # Send payload
    if total_errors > 0:
        payload_text = "\n".join(final_alert_lines)

        if len(payload_text) > MAX_PAYLOAD_CHARS:
            payload_text = (
                payload_text[:MAX_PAYLOAD_CHARS]
                + "\n\n_...truncated — too many broken items for one Slack message. "
                  "Check Looker's Content Validator directly for the full list._"
            )

        webhook_url = os.environ.get("WEBHOOK_URL")
        if webhook_url:
            try:
                # Slack uses specific block formatting for advanced layouts,
                # but standard text payload supports markdown natively.
                response = requests.post(webhook_url, json={"text": payload_text}, timeout=10)
                response.raise_for_status()
                print("Alert sent to webhook.")
            except requests.RequestException as e:
                print(f"Failed to send Slack alert: {e}", file=sys.stderr)
                raise
        else:
            print("WARNING: WEBHOOK_URL is not set — printing report instead of sending to Slack.", file=sys.stderr)
            print(payload_text)
    else:
        print("No broken content found.")


if __name__ == "__main__":
    monitor_and_group_errors()