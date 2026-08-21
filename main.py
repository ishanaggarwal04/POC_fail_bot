import os
import sys
import requests
from datetime import datetime, timezone
from dateutil import parser as dateparser


JIRA_BASE_URL = os.getenv(
    "JIRA_BASE_URL",
    "https://lambdatest.atlassian.net"
)

JIRA_EMAIL = os.getenv(
    "JIRA_EMAIL",
    "your-email@lambdatest.com"
)

JIRA_API_TOKEN = os.getenv(
    "JIRA_API_TOKEN",
    ""
)

JQL = """
project = 11142
AND status IN ("Discovery", "Demo Done")
AND createdDate < "2026-01-01"
"""

LOST_REASON_TEXT = "This POC never reached In Trial status"
LOST_CATEGORY_VALUE = "None"
TARGET_STATUS_NAME = "POC fail"

# Posted instead of the fields/transition when the ticket's most recent
# comment was made in 2026 -- i.e. there's been recent human activity on it,
# so we don't want to silently auto-close it. The Presales Owner is tagged
# with a real @mention; the exact wording is built in
# add_alert_comment() below.

# The year threshold used to decide old vs. recent comments.
CUTOFF_YEAR = 2026

# Name of the fields as they appear in Jira -- resolved to customfield_XXXXX
# ids automatically, no need to hardcode them.
LOST_REASON_FIELD_NAME = "Lost Reason"
LOST_CATEGORY_FIELD_NAME = "Lost Category"
PRESALES_OWNER_FIELD_NAME = "Presales Owner"

# TEMPORARY TEST FILTER: when set, ONLY this issue key will actually be
# updated (everyone else still gets evaluated/printed, just not sent to).
# Set back to None once you're done testing and ready for a real full run.
TEST_ISSUE_ONLY = None

# TEMPORARY TEST LIMIT: when set, only the first N eligible tickets (after
# TEST_ISSUE_ONLY filtering, if any) will actually be updated -- everything
# else still gets evaluated/printed, just not touched. Set back to None once
# you're done testing and ready for a real full run.
LIMIT = 200


session = requests.Session()
session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json"
})

_field_ids = {}
_bot_account_id = None


def jira_get(path, params=None):
    r = session.get(
        f"{JIRA_BASE_URL}{path}",
        params=params
    )
    r.raise_for_status()
    return r.json()


def jira_put(path, payload):
    r = session.put(
        f"{JIRA_BASE_URL}{path}",
        json=payload
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def jira_post(path, payload):
    r = session.post(
        f"{JIRA_BASE_URL}{path}",
        json=payload
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def get_field_id(field_name):
    """Looks up the customfield_XXXXX id for a field by its display name."""

    if field_name in _field_ids:
        return _field_ids[field_name]

    fields = jira_get("/rest/api/3/field")

    for field in fields:
        if field.get("name", "").strip().lower() == field_name.lower():
            _field_ids[field_name] = field["id"]
            return field["id"]

    sys.exit(
        f'Could not find a field named "{field_name}". '
        f"Check the exact field name in Jira (Settings > Issue fields) and "
        f"update the corresponding *_FIELD_NAME constant."
    )


def get_issues():

    issues = []
    next_page_token = None

    presales_owner_field_id = get_field_id(PRESALES_OWNER_FIELD_NAME)

    while True:

        params = {
            "jql": JQL,
            "maxResults": 100,
            "fields": f"summary,status,created,{presales_owner_field_id}"
        }

        if next_page_token:
            params["nextPageToken"] = next_page_token

        data = jira_get(
            "/rest/api/3/search/jql",
            params=params
        )

        issues.extend(
            data.get("issues", [])
        )

        next_page_token = data.get(
            "nextPageToken"
        )

        if not next_page_token:
            break

    return issues


def get_bot_account_id():
    """Returns (and caches) the accountId of the Jira user this script is
    authenticating as -- i.e. whoever JIRA_EMAIL/JIRA_API_TOKEN belongs to.
    Used to recognize the bot's own alert comments so it doesn't re-alert
    on top of itself every batch."""

    global _bot_account_id

    if _bot_account_id is not None:
        return _bot_account_id

    data = jira_get("/rest/api/3/myself")
    _bot_account_id = data["accountId"]

    return _bot_account_id


def get_latest_comment(issue_key):
    """Returns {"year": int, "author_account_id": str or None} for the most
    recent comment on the issue, or None if it has no comments."""

    data = jira_get(
        f"/rest/api/3/issue/{issue_key}/comment",
        params={
            "orderBy": "-created",
            "maxResults": 1
        }
    )

    comments = data.get("comments", [])

    if not comments:
        return None

    latest = comments[0]

    # "created" looks like "2026-07-29T00:31:00.000+0000"
    created_str = latest["created"]
    created_dt = datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S")

    return {
        "year": created_dt.year,
        "author_account_id": latest.get("author", {}).get("accountId")
    }


def get_presales_owner(issue):
    """Returns the {accountId, displayName} of the Presales Owner field, or
    None if it's not set.

    This field is a multi-user picker in Jira, so the API returns a list
    (even with just one person selected). We take the first person.
    """

    presales_owner_field_id = get_field_id(PRESALES_OWNER_FIELD_NAME)
    value = issue["fields"].get(presales_owner_field_id)

    if not value:
        return None

    if isinstance(value, list):
        return value[0] if value else None

    return value  # fallback, in case the field type ever changes


def days_in_current_status(issue):
    """Days since the issue was created -- used as the 'been in
    Discovery/Demo Done for X days' figure in the alert comment, since the
    JQL already restricts us to issues created before 2026 and still
    sitting in one of those two statuses."""

    created_dt = dateparser.isoparse(issue["fields"]["created"])
    return (datetime.now(timezone.utc) - created_dt).days


def build_adf_paragraph(text):
    """Wraps plain text in a minimal Atlassian Document Format body, used
    both for the Lost Reason field and for comments."""

    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": text
                    }
                ]
            }
        ]
    }


def get_transition_id(issue_key, target_status_name):
    """Finds the transition id that moves this issue to TARGET_STATUS_NAME.
    Returns None if no such transition is available from the issue's
    current status (e.g. workflow doesn't allow it directly)."""

    data = jira_get(
        f"/rest/api/3/issue/{issue_key}/transitions"
    )

    for t in data.get("transitions", []):
        to_status = t.get("to", {}).get("name", "")
        if to_status.strip().lower() == target_status_name.lower():
            return t["id"]

    return None


def update_fields(issue_key):
    """Sets Lost Reason and Lost Category on the issue."""

    lost_reason_field_id = get_field_id(LOST_REASON_FIELD_NAME)
    lost_category_field_id = get_field_id(LOST_CATEGORY_FIELD_NAME)

    payload = {
        "fields": {
            lost_reason_field_id: build_adf_paragraph(LOST_REASON_TEXT),
            # Lost Category is a multi-select checkbox field -- represented
            # as a list of {"value": ...} options, even for a single pick.
            lost_category_field_id: [
                {"value": LOST_CATEGORY_VALUE}
            ]
        }
    }

    jira_put(
        f"/rest/api/3/issue/{issue_key}",
        payload
    )


def transition_issue(issue_key, transition_id):

    jira_post(
        f"/rest/api/3/issue/{issue_key}/transitions",
        {
            "transition": {
                "id": transition_id
            }
        }
    )


def add_alert_comment(issue_key, presales_owner, days, current_status):
    """Posts the alert comment, tagging the Presales Owner with a real
    clickable @mention (Jira renders a 'mention' ADF node as an actual
    mention, not plain text) when we have their accountId."""

    content = []

    if presales_owner and presales_owner.get("accountId"):

        content.append({
            "type": "mention",
            "attrs": {
                "id": presales_owner["accountId"]
            }
        })

        content.append({
            "type": "text",
            "text": " "
        })

    else:

        content.append({
            "type": "text",
            "text": "@Presales Owner "
        })

    content.append({
        "type": "text",
        "text": (
            f"please update the status of this POC, as it's been in "
            f"\"{current_status}\" for {days} days."
        )
    })

    jira_post(
        f"/rest/api/3/issue/{issue_key}/comment",
        {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": content
                    }
                ]
            }
        }
    )


def evaluate_issue(issue):
    """Checks one issue, prints what would happen, and returns a dict
    describing the planned action (or None if it can't be processed).

    Two possible actions:
      - "poc_fail": latest comment (if any) is from before CUTOFF_YEAR ->
        set Lost Reason / Lost Category and transition to POC fail, same
        as before.
      - "alert": latest comment is from CUTOFF_YEAR or later -> there's
        recent human activity, so don't touch fields/status, just post a
        comment asking someone to confirm and update manually.
    """

    key = issue["key"]
    current_status = issue["fields"]["status"]["name"]

    presales_owner = get_presales_owner(issue)

    if not presales_owner:

        # No Presales Owner set on the ticket -- there's no one to alert
        # and confirm with, so the comment-recency check doesn't apply.
        # Fall straight through to POC fail based purely on how long it's
        # been sitting in Discovery/Demo Done (same as the original logic,
        # before the comment check existed).
        print(
            f"{key}: currently \"{current_status}\" -> no Presales Owner "
            f"set, skipping comment check"
        )

    else:

        latest_comment = get_latest_comment(key)

        if latest_comment and latest_comment["year"] >= CUTOFF_YEAR:

            if latest_comment["author_account_id"] == get_bot_account_id():

                # The most recent comment is our own previous alert --
                # nobody has responded since. Don't re-alert every batch;
                # just leave it be until a human comments again (which will
                # make THEIR comment the latest one on the next run).
                print(
                    f"{key}: currently \"{current_status}\" -> already "
                    f"alerted and no reply since, skipping"
                )

                return None

            days = days_in_current_status(issue)

            print(
                f"{key}: currently \"{current_status}\" -> latest comment is "
                f"from {latest_comment['year']}, so will post an alert "
                f"comment tagging {presales_owner.get('displayName', '?')} "
                f"instead of touching fields/status"
            )

            return {
                "key": key,
                "action": "alert",
                "presales_owner": presales_owner,
                "days": days,
                "current_status": current_status
            }

    transition_id = get_transition_id(key, TARGET_STATUS_NAME)

    if not transition_id:
        print(
            f"{key}: SKIPPED -- no transition to \"{TARGET_STATUS_NAME}\" "
            f"available from current status \"{current_status}\""
        )
        return None

    print(
        f"{key}: currently \"{current_status}\" -> will set Lost Reason, "
        f"Lost Category = None, and transition to \"{TARGET_STATUS_NAME}\""
    )

    return {
        "key": key,
        "action": "poc_fail",
        "transition_id": transition_id
    }


def main():

    apply = "--apply" in sys.argv

    if not JIRA_API_TOKEN:
        sys.exit(
            "Set JIRA_API_TOKEN."
        )

    issues = get_issues()

    print(
        f"Found {len(issues)} POC(s) matching JQL"
    )

    if TEST_ISSUE_ONLY:

        issues = [
            issue for issue in issues
            if issue["key"] == TEST_ISSUE_ONLY
        ]

        print(
            f"TEST_ISSUE_ONLY is set to '{TEST_ISSUE_ONLY}' -- only "
            f"evaluating that ticket (skipping the rest entirely, not just "
            f"the apply step). Set TEST_ISSUE_ONLY = None to evaluate "
            f"everyone."
        )

        print(
            "Selected tickets: "
            + ", ".join(issue["key"] for issue in issues)
        )

    elif LIMIT is not None:

        issues = issues[:LIMIT]

        print(
            f"LIMIT is set to {LIMIT} -- only evaluating the first "
            f"{len(issues)} of them (skipping the rest entirely, not just "
            f"the apply step). Set LIMIT = None to evaluate everyone."
        )

        print(
            "Selected tickets: "
            + ", ".join(issue["key"] for issue in issues)
        )

    print(
        f"Mode: {'APPLY' if apply else 'DRY RUN'}"
    )

    due = []

    for issue in issues:

        try:

            result = evaluate_issue(issue)

            if result:
                due.append(result)

        except Exception as e:

            print(
                f"{issue['key']}: ERROR - {e}"
            )

    if LIMIT is not None:

        before_count = len(due)

        due = due[:LIMIT]

        print(
            f"\nLIMIT is set to {LIMIT} -- capping {before_count} "
            f"planned update(s) down to {len(due)}. "
            f"Set LIMIT = None to run for everyone."
        )

    poc_fail_count = sum(1 for item in due if item["action"] == "poc_fail")
    alert_count = sum(1 for item in due if item["action"] == "alert")

    print(
        f"\n{len(due)} update(s) due this run "
        f"({poc_fail_count} POC fail, {alert_count} alert comment)."
    )

    for item in due:

        try:

            if item["action"] == "poc_fail":

                print(
                    f" Updating -> {item['key']} (POC fail)"
                )

                if apply:
                    update_fields(item["key"])
                    transition_issue(item["key"], item["transition_id"])

            elif item["action"] == "alert":

                print(
                    f" Commenting -> {item['key']} (alert only, tagging "
                    f"{item['presales_owner'].get('displayName', '?')})"
                )

                if apply:
                    add_alert_comment(
                        item["key"],
                        item["presales_owner"],
                        item["days"],
                        item["current_status"]
                    )

        except Exception as e:

            print(
                f"{item['key']}: ERROR - {e}"
            )


if __name__ == "__main__":
    main()