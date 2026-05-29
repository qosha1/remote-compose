"""
Rate limiting for deployment operations.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from django.core.cache import cache

from ..conf import get_setting
from ..exceptions import ValidationError

logger = logging.getLogger(__name__)


class RateLimitExceeded(ValidationError):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RateLimitInfo:
    """Rate limit status information."""

    allowed: bool
    remaining: int
    limit: int
    reset_at: Optional[float] = None
    retry_after: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "remaining": self.remaining,
            "limit": self.limit,
            "reset_at": self.reset_at,
            "retry_after": self.retry_after,
        }


class RateLimiter:
    """
    Rate limiter using a simplified token bucket algorithm with Django cache backend.

    The token bucket algorithm works as follows:
    - Each bucket has a maximum capacity (limit) of tokens
    - Tokens are consumed when requests are made
    - The bucket is reset after the time window expires
    - If no tokens remain, the request is rate-limited

    This implementation uses a "fixed window" variant where:
    - The window starts with the first request
    - All requests in that window share the same token pool
    - The window resets completely after the timeout

    Supports multiple rate limit strategies:
    - Per-user rate limiting: Limit requests per authenticated user
    - Per-target rate limiting: Limit requests to each deployment target
    - Per-project rate limiting: Limit requests per project name
    - Global rate limiting: Overall system-wide limits

    Cache key format: {prefix}:{key_type}:{hashed_identifier}
    Cache value format: (count, window_start_timestamp)
    """

    def __init__(
        self,
        default_limit: int = 10,
        default_window: int = 60,
        cache_prefix: str = "remote_compose:ratelimit",
    ):
        """
        Initialize rate limiter.

        Args:
            default_limit: Default number of requests allowed per window (bucket capacity)
            default_window: Default time window in seconds (bucket refill period)
            cache_prefix: Prefix for cache keys (for namespacing in shared cache)
        """
        self.default_limit = default_limit
        self.default_window = default_window
        self.cache_prefix = cache_prefix

    def _get_cache_key(self, key_type: str, identifier: str) -> str:
        """Generate cache key for rate limiting."""
        # Hash the identifier for consistent key length
        hashed = hashlib.sha256(identifier.encode()).hexdigest()[:16]
        return f"{self.cache_prefix}:{key_type}:{hashed}"

    def check_rate_limit(
        self,
        key_type: str,
        identifier: str,
        limit: Optional[int] = None,
        window: Optional[int] = None,
    ) -> RateLimitInfo:
        """
        Check if request is within rate limit (without consuming).

        Args:
            key_type: Type of rate limit ('user', 'target', 'project', 'global')
            identifier: Unique identifier for the rate limit bucket
            limit: Number of requests allowed (default: default_limit)
            window: Time window in seconds (default: default_window)

        Returns:
            RateLimitInfo with current status
        """
        limit = limit or self.default_limit
        window = window or self.default_window
        cache_key = self._get_cache_key(key_type, identifier)

        current_data = cache.get(cache_key)

        if current_data is None:
            return RateLimitInfo(
                allowed=True,
                remaining=limit,
                limit=limit,
            )

        count, window_start = current_data
        now = time.time()
        window_end = window_start + window

        if now > window_end:
            # Window expired, reset
            return RateLimitInfo(
                allowed=True,
                remaining=limit,
                limit=limit,
            )

        remaining = max(0, limit - count)
        retry_after = int(window_end - now) if remaining == 0 else None

        return RateLimitInfo(
            allowed=remaining > 0,
            remaining=remaining,
            limit=limit,
            reset_at=window_end,
            retry_after=retry_after,
        )

    def consume(
        self,
        key_type: str,
        identifier: str,
        limit: Optional[int] = None,
        window: Optional[int] = None,
        cost: int = 1,
    ) -> RateLimitInfo:
        """
        Consume tokens from the rate limit bucket.

        This is the main method for rate limiting - it checks if the request
        is allowed and atomically consumes tokens if so.

        Args:
            key_type: Type of rate limit ('user', 'target', 'project', 'global')
            identifier: Unique identifier for the rate limit bucket
            limit: Number of requests allowed (bucket capacity)
            window: Time window in seconds (bucket lifetime)
            cost: Number of tokens to consume (default: 1, use higher for expensive ops)

        Returns:
            RateLimitInfo with updated status showing remaining tokens

        Raises:
            RateLimitExceeded: If rate limit is exceeded (no tokens remaining)
        """
        limit = limit or self.default_limit
        window = window or self.default_window
        cache_key = self._get_cache_key(key_type, identifier)

        now = time.time()
        current_data = cache.get(cache_key)

        # Case 1: No existing bucket - create new one with initial consumption
        if current_data is None:
            # Store (count, window_start) tuple in cache
            # The cache timeout automatically handles bucket expiration
            cache.set(cache_key, (cost, now), timeout=window)
            return RateLimitInfo(
                allowed=True,
                remaining=limit - cost,
                limit=limit,
                reset_at=now + window,
            )

        # Unpack existing bucket state
        count, window_start = current_data
        window_end = window_start + window

        # Case 2: Window has expired - reset the bucket
        if now > window_end:
            cache.set(cache_key, (cost, now), timeout=window)
            return RateLimitInfo(
                allowed=True,
                remaining=limit - cost,
                limit=limit,
                reset_at=now + window,
            )

        # Case 3: Window is active and bucket is empty - reject request
        if count >= limit:
            retry_after = int(window_end - now)
            raise RateLimitExceeded(
                f"Rate limit exceeded. Try again in {retry_after} seconds.",
                retry_after=retry_after,
            )

        # Case 4: Window is active with tokens available - consume and update
        new_count = count + cost
        # Set timeout to remaining window time to ensure cleanup
        remaining_time = int(window_end - now)
        cache.set(cache_key, (new_count, window_start), timeout=remaining_time)

        return RateLimitInfo(
            allowed=True,
            remaining=limit - new_count,
            limit=limit,
            reset_at=window_end,
        )

    def reset(self, key_type: str, identifier: str) -> None:
        """Reset rate limit for a specific key."""
        cache_key = self._get_cache_key(key_type, identifier)
        cache.delete(cache_key)


class DeploymentRateLimiter:
    """
    Specialized rate limiter for deployment operations.
    """

    def __init__(self):
        self.limiter = RateLimiter(
            default_limit=get_setting("RATE_LIMIT_DEPLOYMENTS_PER_MINUTE", 10),
            default_window=60,
            cache_prefix="remote_compose:deploy_ratelimit",
        )

        # Different limits for different operations
        self.limits = {
            "deploy": {
                "limit": get_setting("RATE_LIMIT_DEPLOYMENTS_PER_MINUTE", 10),
                "window": 60,
            },
            "deploy_per_target": {
                "limit": get_setting("RATE_LIMIT_DEPLOYMENTS_PER_TARGET", 5),
                "window": 60,
            },
            "deploy_per_user": {
                "limit": get_setting("RATE_LIMIT_DEPLOYMENTS_PER_USER", 20),
                "window": 300,  # 5 minutes
            },
            "rollback": {
                "limit": get_setting("RATE_LIMIT_ROLLBACKS_PER_MINUTE", 5),
                "window": 60,
            },
        }

    def check_deploy_allowed(
        self,
        target_id: int,
        user: str = "",
        project_name: str = "",
    ) -> Dict[str, RateLimitInfo]:
        """
        Check if deployment is allowed under all rate limits.

        Args:
            target_id: ID of the deployment target
            user: User performing the deployment
            project_name: Project being deployed

        Returns:
            Dict of rate limit checks
        """
        results = {}

        # Check per-target limit
        target_limit = self.limits["deploy_per_target"]
        results["per_target"] = self.limiter.check_rate_limit(
            key_type="target",
            identifier=str(target_id),
            limit=target_limit["limit"],
            window=target_limit["window"],
        )

        # Check per-user limit
        if user:
            user_limit = self.limits["deploy_per_user"]
            results["per_user"] = self.limiter.check_rate_limit(
                key_type="user",
                identifier=user,
                limit=user_limit["limit"],
                window=user_limit["window"],
            )

        # Check global limit
        global_limit = self.limits["deploy"]
        results["global"] = self.limiter.check_rate_limit(
            key_type="global",
            identifier="deployments",
            limit=global_limit["limit"],
            window=global_limit["window"],
        )

        return results

    def consume_deploy(
        self,
        target_id: int,
        user: str = "",
        project_name: str = "",
    ) -> Dict[str, RateLimitInfo]:
        """
        Consume rate limit tokens for a deployment.

        Args:
            target_id: ID of the deployment target
            user: User performing the deployment
            project_name: Project being deployed

        Returns:
            Dict of rate limit results

        Raises:
            RateLimitExceeded: If any rate limit is exceeded
        """
        results = {}

        # Consume per-target limit
        target_limit = self.limits["deploy_per_target"]
        results["per_target"] = self.limiter.consume(
            key_type="target",
            identifier=str(target_id),
            limit=target_limit["limit"],
            window=target_limit["window"],
        )

        # Consume per-user limit
        if user:
            user_limit = self.limits["deploy_per_user"]
            results["per_user"] = self.limiter.consume(
                key_type="user",
                identifier=user,
                limit=user_limit["limit"],
                window=user_limit["window"],
            )

        # Consume global limit
        global_limit = self.limits["deploy"]
        results["global"] = self.limiter.consume(
            key_type="global",
            identifier="deployments",
            limit=global_limit["limit"],
            window=global_limit["window"],
        )

        logger.info(
            f"Rate limit consumed for deployment to target {target_id} by {user}"
        )

        return results

    def consume_rollback(self, target_id: int, user: str = "") -> RateLimitInfo:
        """
        Consume rate limit token for a rollback.

        Args:
            target_id: ID of the deployment target
            user: User performing the rollback

        Returns:
            RateLimitInfo

        Raises:
            RateLimitExceeded: If rate limit is exceeded
        """
        rollback_limit = self.limits["rollback"]
        return self.limiter.consume(
            key_type="rollback",
            identifier=str(target_id),
            limit=rollback_limit["limit"],
            window=rollback_limit["window"],
        )

    def get_status(
        self,
        target_id: Optional[int] = None,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get current rate limit status.

        Args:
            target_id: Optional target ID
            user: Optional user

        Returns:
            Dict with rate limit status
        """
        status = {}

        if target_id:
            target_limit = self.limits["deploy_per_target"]
            status["target"] = self.limiter.check_rate_limit(
                key_type="target",
                identifier=str(target_id),
                limit=target_limit["limit"],
                window=target_limit["window"],
            ).to_dict()

        if user:
            user_limit = self.limits["deploy_per_user"]
            status["user"] = self.limiter.check_rate_limit(
                key_type="user",
                identifier=user,
                limit=user_limit["limit"],
                window=user_limit["window"],
            ).to_dict()

        global_limit = self.limits["deploy"]
        status["global"] = self.limiter.check_rate_limit(
            key_type="global",
            identifier="deployments",
            limit=global_limit["limit"],
            window=global_limit["window"],
        ).to_dict()

        return status
