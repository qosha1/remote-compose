"""
Health monitoring example.

This example demonstrates:
- Checking target server health
- Monitoring deployment health
- Running custom health checks
- Setting up automated health monitoring
"""

from remote_compose.services import HealthService, HealthCheckResult, HealthReport
from remote_compose.models import DeploymentTarget, Deployment


def check_single_target():
    """Check health of a single target server."""
    health_service = HealthService()
    target = DeploymentTarget.objects.get(name='production-server')

    result = health_service.check_target_health(target)

    print(f"Target: {result.target_name}")
    print(f"Healthy: {result.healthy}")
    print(f"Message: {result.message}")

    if result.details:
        print(f"Details: {result.details}")

    return result


def check_all_targets():
    """Check health of all active targets."""
    health_service = HealthService()

    # Check all active targets
    report = health_service.check_all_targets_health(only_active=True)

    print(f"Overall Healthy: {report.overall_healthy}")
    print(f"Total Checked: {report.total_checked}")
    print(f"Healthy: {report.healthy_count}")
    print(f"Unhealthy: {report.unhealthy_count}")

    # Print details for unhealthy targets
    print("\nUnhealthy Targets:")
    for result in report.results:
        if not result.healthy:
            print(f"  - {result.target_name}: {result.message}")

    return report


def check_deployment_health():
    """Check health of a specific deployment."""
    health_service = HealthService()

    # Get latest deployment
    deployment = Deployment.objects.filter(
        status=Deployment.Status.SUCCESS
    ).order_by('-completed_at').first()

    if not deployment:
        print("No successful deployments found")
        return None

    result = health_service.check_deployment_health(deployment)

    print(f"Deployment: {deployment.project_name} (ID: {deployment.id})")
    print(f"Target: {result.target_name}")
    print(f"Healthy: {result.healthy}")
    print(f"Message: {result.message}")

    if result.details and 'services' in result.details:
        print("\nService Status:")
        for service_name, info in result.details['services'].items():
            status = "Running" if info.get('healthy') else "Not Running"
            print(f"  - {service_name}: {status}")

    return result


def check_all_deployments():
    """Check health of all successful deployments."""
    health_service = HealthService()

    report = health_service.check_all_deployments_health()

    print(f"Overall Healthy: {report.overall_healthy}")
    print(f"Total Deployments Checked: {report.total_checked}")

    # Group by health status
    print("\nDeployment Health:")
    for result in report.results:
        status = "HEALTHY" if result.healthy else "UNHEALTHY"
        print(f"  [{status}] {result.project_name} @ {result.target_name}")

    return report


def run_custom_health_check():
    """Run a custom health check command on a deployment."""
    health_service = HealthService()

    deployment = Deployment.objects.filter(
        status=Deployment.Status.SUCCESS
    ).first()

    if not deployment:
        print("No successful deployments found")
        return None

    # Custom health check - check if web service responds
    result = health_service.run_custom_health_check(
        deployment=deployment,
        command='curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health',
        expected_output='200',  # Expect HTTP 200
        expected_exit_code=0,
    )

    print(f"Custom Health Check Result:")
    print(f"  Healthy: {result.healthy}")
    print(f"  Message: {result.message}")

    if result.details:
        print(f"  Exit Code: {result.details.get('exit_code')}")
        print(f"  Output: {result.details.get('stdout', '')[:100]}")

    return result


def get_unhealthy_targets():
    """Get list of unhealthy targets."""
    health_service = HealthService()

    unhealthy = health_service.get_unhealthy_targets()

    print(f"Unhealthy Targets ({unhealthy.count()}):")
    for target in unhealthy:
        print(f"  - {target.name} ({target.host})")
        print(f"    Last Check: {target.last_health_check}")
        print(f"    Status: {target.health_status}")

    return unhealthy


def find_stale_deployments():
    """Find deployments that have been running too long."""
    health_service = HealthService()

    # Find deployments running for more than 24 hours
    stale = health_service.get_stale_deployments(max_running_hours=24)

    print(f"Stale Deployments ({stale.count()}):")
    for deployment in stale:
        duration = (deployment.started_at - deployment.created_at).total_seconds() / 3600
        print(f"  - {deployment.project_name} @ {deployment.target.name}")
        print(f"    Running for: {duration:.1f} hours")
        print(f"    Started: {deployment.started_at}")

    return stale


def get_health_history():
    """Get health check history for a target."""
    health_service = HealthService()
    target = DeploymentTarget.objects.first()

    if not target:
        print("No targets found")
        return []

    # Get last 24 hours of health history
    history = health_service.get_health_history(target, hours=24)

    print(f"Health History for {target.name} (last 24h):")
    for entry in history[:10]:  # Show last 10 entries
        print(f"  [{entry['timestamp']}] {entry['message']}")

    return history


def setup_health_monitoring():
    """
    Example Celery beat configuration for automated health monitoring.

    Add this to your Django settings or celeryconfig.py:
    """
    celery_beat_schedule = {
        # Check all targets every 5 minutes
        'health-check-targets': {
            'task': 'remote_compose.tasks.check_all_targets_health',
            'schedule': 300.0,  # 5 minutes
        },

        # Check deployment health every 10 minutes
        'health-check-deployments': {
            'task': 'remote_compose.tasks.run_health_checks',
            'schedule': 600.0,  # 10 minutes
        },

        # Monitor for stale deployments hourly
        'monitor-stale-deployments': {
            'task': 'remote_compose.tasks.monitor_stale_deployments',
            'schedule': 3600.0,  # 1 hour
            'kwargs': {'max_running_hours': 24},
        },
    }

    print("Celery beat schedule for health monitoring:")
    for name, config in celery_beat_schedule.items():
        print(f"  {name}: every {config['schedule']}s")

    return celery_beat_schedule


if __name__ == '__main__':
    print("=" * 50)
    print("Target Health Check")
    print("=" * 50)
    check_all_targets()

    print("\n" + "=" * 50)
    print("Deployment Health Check")
    print("=" * 50)
    check_all_deployments()

    print("\n" + "=" * 50)
    print("Unhealthy Targets")
    print("=" * 50)
    get_unhealthy_targets()
