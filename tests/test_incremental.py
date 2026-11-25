import pytest

from wechat_vault.incremental import AlreadyRunning, choose_since, cloud_watermark, single_instance

NOW = 1_800_000_000
DAY = 86400


def choose(**over):
    args = {"full": False, "since": 0, "watermark": NOW - DAY, "lookback_days": 7, "now": NOW}
    return choose_since(**{**args, **over})


def test_since_starts_at_the_watermark_minus_the_lookback():
    assert choose() == NOW - DAY - 7 * DAY


def test_full_and_explicit_since_override_the_watermark():
    assert choose(full=True) == 0
    assert choose(since=123) == 123


def test_future_watermark_is_clamped_to_now():
    assert choose(watermark=NOW + 30 * DAY) == NOW - 7 * DAY


def test_missing_watermark_requires_an_explicit_choice():
    with pytest.raises(ValueError):
        choose(watermark=0)


def test_cloud_watermark_is_per_source():
    class Store:
        def scan(self, table, **kwargs):
            assert table == "wx_sync_state"
            return iter([
                {"source_id": "win", "last_create_time": 10},
                {"source_id": "mac", "last_create_time": 99},
                {"source_id": "win", "last_create_time": 30},
                {"source_id": "win", "last_create_time": None},
            ])

    assert cloud_watermark(Store(), "win") == 30
    assert cloud_watermark(Store(), "other") == 0


def test_second_run_is_refused_while_the_first_holds_the_lock():
    with single_instance("wechat-vault-test-lock"):
        with pytest.raises(AlreadyRunning), single_instance("wechat-vault-test-lock"):
            pass
    with single_instance("wechat-vault-test-lock"):
        pass
