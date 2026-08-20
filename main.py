import os
import sys
import requests


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

# Name of the fields as they appear in Jira -- resolved to customfield_XXXXX
# ids automatically, no need to hardcode them.
LOST_REASON_FIELD_NAME = "Lost Reason"
LOST_CATEGORY_FIELD_NAME = "Lost Category"

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

    while True:

        params = {
            "jql": JQL,
            "maxResults": 100,
            "fields": "summary,status"
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


def build_lost_reason_adf(text):
    """Lost Reason is a rich-text field, so it needs an Atlassian Document
    Format body rather than a plain string."""

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
            lost_reason_field_id: build_lost_reason_adf(LOST_REASON_TEXT),
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


def evaluate_issue(issue):
    """Checks one issue, prints what would happen, and returns a dict
    describing the planned update (or None if it can't be processed, e.g.
    no valid transition to the target status exists)."""

    key = issue["key"]
    current_status = issue["fields"]["status"]["name"]

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

    print(
        f"\n{len(due)} update(s) due this run."
    )

    for item in due:

        try:

            print(
                f" Updating -> {item['key']}"
            )

            if apply:

                update_fields(item["key"])
                transition_issue(item["key"], item["transition_id"])

        except Exception as e:

            print(
                f"{item['key']}: ERROR - {e}"
            )


if __name__ == "__main__":
    main()