"""Tests for content_validation.get_time_bucket.

looker_sdk is stubbed so the module imports without the real SDK or any
Looker credentials. init40() lives inside monitor_and_group_errors(), so
merely importing the module never touches the network.
"""

import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# Stub looker_sdk before importing the module under test.
sys.modules.setdefault("looker_sdk", types.ModuleType("looker_sdk"))

# Make the repo root importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from content_validation import get_time_bucket  # noqa: E402


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_never_viewed_returns_old_bucket():
    assert get_time_bucket(None, NOW) == "older than 1 month / never viewed"


@pytest.mark.parametrize(
    "days_ago, expected",
    [
        (0, "last 1 week"),
        (7, "last 1 week"),
        (8, "last 2 weeks"),
        (14, "last 2 weeks"),
        (15, "last 1 month"),
        (30, "last 1 month"),
        (31, "older than 1 month / never viewed"),
        (365, "older than 1 month / never viewed"),
    ],
)
def test_bucket_boundaries(days_ago, expected):
    last_viewed = NOW - timedelta(days=days_ago)
    assert get_time_bucket(last_viewed, NOW) == expected


def test_naive_datetime_is_treated_as_utc():
    naive = (NOW - timedelta(days=3)).replace(tzinfo=None)
    assert get_time_bucket(naive, NOW) == "last 1 week"


def test_never_viewed_and_old_share_the_same_bucket_key():
    # Regression guard: "never viewed" and "older than 1 month" must map to
    # the exact same string so never-viewed content isn't dropped from the
    # rendered report.
    old = NOW - timedelta(days=90)
    assert get_time_bucket(None, NOW) == get_time_bucket(old, NOW)
