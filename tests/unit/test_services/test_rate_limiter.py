"""
Unit tests for RateLimiter and DeploymentRateLimiter.
"""

import pytest
from unittest.mock import patch
import time

from remote_compose.services import (
    RateLimiter,
    DeploymentRateLimiter,
    RateLimitExceeded,
    RateLimitInfo,
)


class TestRateLimiter:
    """Tests for the generic RateLimiter."""

    @pytest.fixture
    def limiter(self):
        return RateLimiter(default_limit=5, default_window=60)

    def test_check_rate_limit_allows_first_request(self, limiter):
        """Test that first request is allowed."""
        with patch("django.core.cache.cache.get", return_value=None):
            result = limiter.check_rate_limit("test", "user1")

        assert result.allowed is True
        assert result.remaining == 5
        assert result.limit == 5

    def test_check_rate_limit_tracks_usage(self, limiter):
        """Test that usage is tracked correctly."""
        # Simulate 3 requests already made
        with patch("django.core.cache.cache.get", return_value=(3, time.time())):
            result = limiter.check_rate_limit("test", "user1")

        assert result.allowed is True
        assert result.remaining == 2

    def test_check_rate_limit_blocks_when_exceeded(self, limiter):
        """Test that requests are blocked when limit exceeded."""
        # Simulate limit exceeded
        with patch("django.core.cache.cache.get", return_value=(5, time.time())):
            result = limiter.check_rate_limit("test", "user1")

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None

    def test_consume_first_request(self, limiter):
        """Test consuming first request."""
        with (
            patch("django.core.cache.cache.get", return_value=None),
            patch("django.core.cache.cache.set") as mock_set,
        ):
            result = limiter.consume("test", "user1")

        assert result.allowed is True
        assert result.remaining == 4
        mock_set.assert_called_once()

    def test_consume_raises_when_exceeded(self, limiter):
        """Test that consume raises when limit exceeded."""
        with patch("django.core.cache.cache.get", return_value=(5, time.time())):
            with pytest.raises(RateLimitExceeded) as exc_info:
                limiter.consume("test", "user1")

        assert exc_info.value.retry_after is not None

    def test_consume_resets_after_window(self, limiter):
        """Test that window resets after expiry."""
        # Window started 61 seconds ago (expired)
        old_time = time.time() - 61
        with (
            patch("django.core.cache.cache.get", return_value=(5, old_time)),
            patch("django.core.cache.cache.set"),
        ):
            result = limiter.consume("test", "user1")

        assert result.allowed is True
        assert result.remaining == 4

    def test_reset_clears_cache(self, limiter):
        """Test that reset clears the cache."""
        with patch("django.core.cache.cache.delete") as mock_delete:
            limiter.reset("test", "user1")

        mock_delete.assert_called_once()


class TestDeploymentRateLimiter:
    """Tests for the DeploymentRateLimiter."""

    @pytest.fixture
    def deployment_limiter(self):
        return DeploymentRateLimiter()

    def test_check_deploy_allowed_returns_all_limits(self, deployment_limiter):
        """Test that check returns status for all limit types."""
        with patch("django.core.cache.cache.get", return_value=None):
            result = deployment_limiter.check_deploy_allowed(
                target_id=1,
                user="testuser",
            )

        assert "per_target" in result
        assert "per_user" in result
        assert "global" in result

    def test_consume_deploy_updates_all_limits(self, deployment_limiter):
        """Test that consume updates all limit buckets."""
        with (
            patch("django.core.cache.cache.get", return_value=None),
            patch("django.core.cache.cache.set") as mock_set,
        ):
            deployment_limiter.consume_deploy(
                target_id=1,
                user="testuser",
            )

        # Should set cache for target, user, and global
        assert mock_set.call_count == 3

    def test_consume_rollback(self, deployment_limiter):
        """Test rollback rate limiting."""
        with (
            patch("django.core.cache.cache.get", return_value=None),
            patch("django.core.cache.cache.set"),
        ):
            result = deployment_limiter.consume_rollback(
                target_id=1,
                user="testuser",
            )

        assert result.allowed is True

    def test_get_status(self, deployment_limiter):
        """Test getting rate limit status."""
        with patch("django.core.cache.cache.get", return_value=None):
            status = deployment_limiter.get_status(target_id=1, user="testuser")

        assert "target" in status
        assert "user" in status
        assert "global" in status


class TestRateLimitInfo:
    """Tests for RateLimitInfo dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        info = RateLimitInfo(
            allowed=True,
            remaining=5,
            limit=10,
            reset_at=1234567890.0,
            retry_after=None,
        )

        result = info.to_dict()

        assert result["allowed"] is True
        assert result["remaining"] == 5
        assert result["limit"] == 10
