"""
Rate limiting example.

This example demonstrates:
- Checking rate limit status
- Consuming rate limit tokens
- Handling rate limit exceeded errors
- Using deployment-specific rate limiting
"""

from remote_compose.services import RateLimiter, DeploymentRateLimiter
from remote_compose.services.rate_limiter import RateLimitExceeded


def basic_rate_limiting():
    """Basic rate limiter usage."""
    # Create a rate limiter: 10 requests per 60 seconds
    limiter = RateLimiter(default_limit=10, default_window=60)

    # Check current status without consuming
    status = limiter.check_rate_limit(
        key_type='user',
        identifier='user@example.com',
    )

    print(f"Rate Limit Status:")
    print(f"  Allowed: {status.allowed}")
    print(f"  Remaining: {status.remaining}/{status.limit}")

    if status.retry_after:
        print(f"  Retry After: {status.retry_after}s")


def consume_rate_limit():
    """Consume rate limit tokens."""
    limiter = RateLimiter(default_limit=5, default_window=60)

    try:
        # Consume one token
        result = limiter.consume(
            key_type='api',
            identifier='my-api-key',
        )

        print(f"Request allowed!")
        print(f"  Remaining: {result.remaining}/{result.limit}")
        print(f"  Resets at: {result.reset_at}")

    except RateLimitExceeded as e:
        print(f"Rate limit exceeded!")
        print(f"  Message: {e}")
        print(f"  Retry after: {e.retry_after}s")


def consume_multiple_tokens():
    """Consume multiple tokens for expensive operations."""
    limiter = RateLimiter(default_limit=100, default_window=300)

    try:
        # Expensive operation costs 10 tokens
        result = limiter.consume(
            key_type='expensive_op',
            identifier='user123',
            cost=10,  # Cost 10 tokens instead of 1
        )

        print(f"Expensive operation allowed")
        print(f"  Remaining tokens: {result.remaining}")

    except RateLimitExceeded as e:
        print(f"Rate limit exceeded for expensive operation")


def custom_limits():
    """Use custom limits for specific operations."""
    limiter = RateLimiter()

    # Different limits for different operations
    limits_config = {
        'read': {'limit': 100, 'window': 60},    # 100 reads per minute
        'write': {'limit': 20, 'window': 60},    # 20 writes per minute
        'delete': {'limit': 5, 'window': 300},   # 5 deletes per 5 minutes
    }

    operation = 'write'
    config = limits_config[operation]

    try:
        result = limiter.consume(
            key_type=operation,
            identifier='user@example.com',
            limit=config['limit'],
            window=config['window'],
        )
        print(f"{operation.title()} operation allowed")
        print(f"  Remaining {operation}s: {result.remaining}")

    except RateLimitExceeded as e:
        print(f"{operation.title()} rate limit exceeded: {e}")


def deployment_rate_limiting():
    """Use specialized deployment rate limiter."""
    limiter = DeploymentRateLimiter()

    target_id = 1
    user = 'admin@example.com'

    # Check if deployment is allowed
    status = limiter.check_deploy_allowed(
        target_id=target_id,
        user=user,
        project_name='myapp',
    )

    print("Deployment Rate Limit Status:")
    print(f"  Per-Target: {status['per_target'].remaining} remaining")

    if 'per_user' in status:
        print(f"  Per-User: {status['per_user'].remaining} remaining")

    print(f"  Global: {status['global'].remaining} remaining")

    # Check if all limits allow the deployment
    all_allowed = all(info.allowed for info in status.values())

    if all_allowed:
        print("\nDeployment is allowed!")
    else:
        print("\nDeployment is NOT allowed - rate limit exceeded")
        for limit_type, info in status.items():
            if not info.allowed:
                print(f"  {limit_type}: retry after {info.retry_after}s")


def consume_deployment_limit():
    """Consume deployment rate limit tokens."""
    limiter = DeploymentRateLimiter()

    try:
        # This will consume tokens from all relevant buckets
        result = limiter.consume_deploy(
            target_id=1,
            user='admin@example.com',
            project_name='myapp',
        )

        print("Deployment rate limit consumed:")
        for limit_type, info in result.items():
            print(f"  {limit_type}: {info.remaining} remaining")

    except RateLimitExceeded as e:
        print(f"Deployment rate limit exceeded: {e}")
        print(f"Retry after: {e.retry_after}s")


def get_rate_limit_status():
    """Get current rate limit status for a user/target."""
    limiter = DeploymentRateLimiter()

    status = limiter.get_status(
        target_id=1,
        user='admin@example.com',
    )

    print("Current Rate Limit Status:")
    for limit_type, info in status.items():
        print(f"\n{limit_type.title()}:")
        print(f"  Allowed: {info['allowed']}")
        print(f"  Remaining: {info['remaining']}/{info['limit']}")
        if info.get('retry_after'):
            print(f"  Retry After: {info['retry_after']}s")


def reset_rate_limit():
    """Reset rate limit for a specific key (admin operation)."""
    limiter = RateLimiter()

    # Reset rate limit for a specific user
    limiter.reset(
        key_type='user',
        identifier='user@example.com',
    )

    print("Rate limit reset for user@example.com")


def handle_rate_limit_in_deployment():
    """
    Example of handling rate limits in a deployment workflow.

    This shows the recommended pattern for checking and handling
    rate limits before attempting a deployment.
    """
    from remote_compose.services import DeploymentService
    from remote_compose.models import DeploymentTarget

    limiter = DeploymentRateLimiter()
    deployment_service = DeploymentService()

    target_id = 1
    user = 'admin@example.com'

    # 1. Check if deployment is allowed before starting
    status = limiter.check_deploy_allowed(target_id=target_id, user=user)

    all_allowed = all(info.allowed for info in status.values())

    if not all_allowed:
        # Find which limit is blocking
        for limit_type, info in status.items():
            if not info.allowed:
                print(f"Rate limit exceeded: {limit_type}")
                print(f"Please wait {info.retry_after} seconds")
                return None

    # 2. Consume the rate limit
    try:
        limiter.consume_deploy(target_id=target_id, user=user)
    except RateLimitExceeded as e:
        print(f"Rate limit exceeded during consumption: {e}")
        return None

    # 3. Proceed with deployment
    target = DeploymentTarget.objects.get(id=target_id)

    deployment = deployment_service.deploy(
        target=target,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        deployed_by=user,
    )

    print(f"Deployment started: {deployment.id}")
    return deployment


if __name__ == '__main__':
    print("=" * 50)
    print("Basic Rate Limiting")
    print("=" * 50)
    basic_rate_limiting()

    print("\n" + "=" * 50)
    print("Deployment Rate Limiting")
    print("=" * 50)
    deployment_rate_limiting()

    print("\n" + "=" * 50)
    print("Rate Limit Status")
    print("=" * 50)
    get_rate_limit_status()
