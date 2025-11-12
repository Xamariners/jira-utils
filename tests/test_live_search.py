import os
from types import SimpleNamespace

import pytest

from jira_utils.cli import collect_attachments
from jira_utils.config import load_config
from jira_utils.jira_client import JiraClient

CONFIG_ENV = "JIRA_LIVE_CONFIG"


@pytest.mark.live
def test_live_attachment_listing_round_trip() -> None:
    """
    Live test (opt-in) that exercises the Jira API end-to-end.

    Run with:
        JIRA_LIVE_CONFIG=config.json pytest tests/test_live_search.py -k live -s
    Optionally override defaults:
        JIRA_LIVE_PROJECTS="AF OPS" JIRA_LIVE_BEFORE=2025-01-01 JIRA_LIVE_MIN_SIZE_MB=1
    """

    config_path = os.environ.get(CONFIG_ENV)
    if not config_path:
        pytest.skip(f"Set {CONFIG_ENV}=path/to/config.json to run live tests.")

    config = load_config(config_path)
    before = os.environ.get("JIRA_LIVE_BEFORE", "2100-01-01")
    min_size_mb = float(os.environ.get("JIRA_LIVE_MIN_SIZE_MB", "0"))
    projects = os.environ.get("JIRA_LIVE_PROJECTS")
    project_list = projects.split() if projects else None

    args = SimpleNamespace(
        before=before,
        min_size_mb=min_size_mb,
        projects=project_list,
        max_issues=int(os.environ.get("JIRA_LIVE_MAX_ISSUES", "100")),
        attachment_ids=None,
        verbose=True,
    )

    with JiraClient(config) as client:
        records = collect_attachments(client, config, args)

    print(f"Collected {len(records)} attachment candidates.")
    for record in records[:5]:
        print(
            f"- {record.issue_key} {record.attachment_name} "
            f"{record.size_mb:.2f}MB created {record.created_iso}"
        )

    assert records, "No attachments matched the provided filters."
