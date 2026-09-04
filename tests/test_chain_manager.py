"""Tests for chain_manager stream-recovery helpers (pure functions)."""

from chain_manager import chain_stream_status_line, unknown_retry_due

NOW = 1000.0
COOLDOWN = 120.0


class TestUnknownRetryDue:
    """Blacklisted qualification keys retry once the cooldown elapses."""

    def test_empty_blacklist_yields_nothing(self):
        assert unknown_retry_due({}, NOW, COOLDOWN) == set()

    def test_recent_failure_stays_blacklisted(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - 1.0}, NOW, COOLDOWN) == set()

    def test_failure_at_exact_cooldown_is_retryable(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - COOLDOWN}, NOW, COOLDOWN) == {key}

    def test_old_failure_is_retryable(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - COOLDOWN - 60.0}, NOW, COOLDOWN) == {key}

    def test_mixed_freshness_returns_only_stale_keys(self):
        fresh = (6000.0, "C")
        stale = (6005.0, "P")
        unknown = {fresh: NOW - 5.0, stale: NOW - COOLDOWN - 5.0}
        assert unknown_retry_due(unknown, NOW, COOLDOWN) == {stale}

    def test_default_cooldown_comes_from_config(self):
        from config import CHAIN_STREAM_UNKNOWN_RETRY_SECS
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - CHAIN_STREAM_UNKNOWN_RETRY_SECS}, NOW) == {key}


class TestChainStreamStatusLine:
    """The 10s status log explains itself when quotes are not flowing."""

    def test_quotes_present_log_unchanged(self):
        assert chain_stream_status_line(96, 40, 96) == "Chain stream ticks: 96 contracts, quotes_present=40, active_subs=96"

    def test_zero_quotes_adds_recovery_hint(self):
        line = chain_stream_status_line(96, 0, 96)
        assert "quotes_present=0" in line
        assert "2157" in line  # sec-def farm outage signature

    def test_zero_quotes_with_no_subs_gets_no_hint(self):
        assert chain_stream_status_line(0, 0, 0) == "Chain stream ticks: 0 contracts, quotes_present=0, active_subs=0"

