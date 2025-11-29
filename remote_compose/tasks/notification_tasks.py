"""
Celery tasks for notifications and webhooks.
"""

import json
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from celery import shared_task
from django.utils import timezone

from ..models import Deployment, DeploymentTarget
from ..conf import get_setting

logger = logging.getLogger(__name__)

# Safe domains that can receive webhooks (can be configured)
ALLOWED_WEBHOOK_DOMAINS = frozenset({
    'localhost',
    '127.0.0.1',
    # Add trusted webhook domains here
})


def _validate_webhook_url(url: str) -> bool:
    """
    Validate webhook URL for safety.

    Prevents SSRF attacks by validating the URL.
    """
    try:
        parsed = urlparse(url)

        # Must be http or https
        if parsed.scheme not in ('http', 'https'):
            return False

        # Check if domain is allowed (or if all domains allowed via setting)
        allow_all = get_setting('WEBHOOK_ALLOW_ALL_DOMAINS', False)
        if not allow_all:
            allowed_domains = get_setting('WEBHOOK_ALLOWED_DOMAINS', ALLOWED_WEBHOOK_DOMAINS)
            if parsed.hostname not in allowed_domains:
                logger.warning(f"Webhook domain not allowed: {parsed.hostname}")
                return False

        # Block private IP ranges (basic check)
        if not allow_all:
            hostname = parsed.hostname or ''
            if hostname.startswith(('10.', '172.', '192.168.', '169.254.')):
                logger.warning(f"Webhook to private IP not allowed: {hostname}")
                return False

        return True

    except Exception as e:
        logger.error(f"Invalid webhook URL: {url}, error: {e}")
        return False


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(ConnectionError, TimeoutError),
)
def send_webhook(
    self,
    webhook_url: str,
    event: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> dict:
    """
    Send a webhook notification.

    Args:
        webhook_url: URL to send webhook to
        event: Event type (e.g., 'deployment.completed')
        payload: Event payload
        headers: Optional additional headers
        timeout: Request timeout in seconds

    Returns:
        Dict with send result
    """
    import requests

    if not _validate_webhook_url(webhook_url):
        return {
            'success': False,
            'error': 'Invalid or disallowed webhook URL',
            'url': webhook_url,
        }

    webhook_payload = {
        'event': event,
        'timestamp': timezone.now().isoformat(),
        'data': payload,
    }

    default_headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'remote-compose-webhook/1.0',
        'X-Webhook-Event': event,
    }

    if headers:
        default_headers.update(headers)

    try:
        response = requests.post(
            webhook_url,
            json=webhook_payload,
            headers=default_headers,
            timeout=timeout,
        )

        response.raise_for_status()

        logger.info(f"Webhook sent successfully to {webhook_url}: {event}")

        return {
            'success': True,
            'url': webhook_url,
            'event': event,
            'status_code': response.status_code,
        }

    except requests.exceptions.Timeout:
        logger.warning(f"Webhook timeout: {webhook_url}")
        raise self.retry(exc=TimeoutError(f"Webhook timeout: {webhook_url}"))

    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook failed: {webhook_url}, error: {e}")
        return {
            'success': False,
            'url': webhook_url,
            'event': event,
            'error': str(e),
        }


@shared_task(bind=True)
def send_deployment_notification(
    self,
    deployment_id: int,
    event: str,
    channels: Optional[List[str]] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Send deployment notification to configured channels.

    Supports multiple notification channels: webhooks, Slack, email, etc.

    Args:
        deployment_id: ID of the Deployment
        event: Event type
        channels: Optional list of channels to use (default: all configured)
        extra_data: Optional extra data to include

    Returns:
        Dict with notification results
    """
    try:
        deployment = Deployment.objects.select_related('target', 'context').get(id=deployment_id)
    except Deployment.DoesNotExist:
        return {
            'success': False,
            'error': f"Deployment {deployment_id} not found",
        }

    # Build notification payload
    payload = {
        'deployment_id': deployment.id,
        'project_name': deployment.project_name,
        'version': deployment.version,
        'status': deployment.status,
        'target': {
            'id': deployment.target.id,
            'name': deployment.target.name,
            'host': deployment.target.host,
        },
        'deployed_by': deployment.deployed_by,
        'started_at': deployment.started_at.isoformat() if deployment.started_at else None,
        'completed_at': deployment.completed_at.isoformat() if deployment.completed_at else None,
        'duration_seconds': deployment.duration,
        'error_message': deployment.error_message,
    }

    if extra_data:
        payload['extra'] = extra_data

    results = []
    channels = channels or get_setting('NOTIFICATION_CHANNELS', [])

    # Send to webhook channels
    webhook_urls = get_setting('NOTIFICATION_WEBHOOK_URLS', [])
    if 'webhook' in channels or not channels:
        for url in webhook_urls:
            result = send_webhook.delay(
                webhook_url=url,
                event=event,
                payload=payload,
            )
            results.append({
                'channel': 'webhook',
                'url': url,
                'task_id': result.id,
            })

    # Send to Slack
    slack_webhook = get_setting('SLACK_WEBHOOK_URL')
    if slack_webhook and ('slack' in channels or not channels):
        slack_result = send_slack_notification.delay(
            deployment_id=deployment_id,
            event=event,
            payload=payload,
        )
        results.append({
            'channel': 'slack',
            'task_id': slack_result.id,
        })

    return {
        'success': True,
        'deployment_id': deployment_id,
        'event': event,
        'notifications_sent': len(results),
        'results': results,
    }


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def send_slack_notification(
    self,
    deployment_id: int,
    event: str,
    payload: Dict[str, Any],
) -> dict:
    """
    Send notification to Slack.

    Args:
        deployment_id: ID of the Deployment
        event: Event type
        payload: Notification payload

    Returns:
        Dict with send result
    """
    import requests

    slack_webhook = get_setting('SLACK_WEBHOOK_URL')
    if not slack_webhook:
        return {
            'success': False,
            'error': 'Slack webhook URL not configured',
        }

    # Build Slack message
    status = payload.get('status', 'unknown')
    color = {
        'success': 'good',
        'failed': 'danger',
        'running': 'warning',
        'pending': '#439FE0',
    }.get(status, '#808080')

    emoji = {
        'deployment.completed': ':white_check_mark:',
        'deployment.failed': ':x:',
        'deployment.started': ':rocket:',
        'rollback.completed': ':rewind:',
        'rollback.failed': ':warning:',
    }.get(event, ':information_source:')

    slack_message = {
        'attachments': [{
            'color': color,
            'title': f"{emoji} Deployment {event.split('.')[-1].title()}",
            'fields': [
                {
                    'title': 'Project',
                    'value': payload.get('project_name', 'N/A'),
                    'short': True,
                },
                {
                    'title': 'Version',
                    'value': payload.get('version', 'N/A'),
                    'short': True,
                },
                {
                    'title': 'Target',
                    'value': payload.get('target', {}).get('name', 'N/A'),
                    'short': True,
                },
                {
                    'title': 'Status',
                    'value': status.upper(),
                    'short': True,
                },
                {
                    'title': 'Deployed By',
                    'value': payload.get('deployed_by', 'N/A'),
                    'short': True,
                },
                {
                    'title': 'Duration',
                    'value': f"{payload.get('duration_seconds', 0):.1f}s",
                    'short': True,
                },
            ],
            'footer': 'remote-compose',
            'ts': int(timezone.now().timestamp()),
        }],
    }

    # Add error message if present
    error_message = payload.get('error_message')
    if error_message:
        slack_message['attachments'][0]['fields'].append({
            'title': 'Error',
            'value': error_message[:200],  # Truncate long errors
            'short': False,
        })

    try:
        response = requests.post(
            slack_webhook,
            json=slack_message,
            timeout=30,
        )
        response.raise_for_status()

        return {
            'success': True,
            'channel': 'slack',
            'deployment_id': deployment_id,
        }

    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
        raise self.retry(exc=e)


@shared_task
def send_email_notification(
    deployment_id: int,
    event: str,
    recipients: List[str],
) -> dict:
    """
    Send email notification for deployment events.

    Args:
        deployment_id: ID of the Deployment
        event: Event type
        recipients: List of email addresses

    Returns:
        Dict with send result
    """
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        deployment = Deployment.objects.select_related('target').get(id=deployment_id)
    except Deployment.DoesNotExist:
        return {
            'success': False,
            'error': f"Deployment {deployment_id} not found",
        }

    status = deployment.status
    subject = f"[remote-compose] {deployment.project_name} - {event.split('.')[-1].title()}"

    message = f"""
Deployment Notification
=======================

Event: {event}
Project: {deployment.project_name}
Version: {deployment.version}
Target: {deployment.target.name} ({deployment.target.host})
Status: {status}
Deployed By: {deployment.deployed_by or 'N/A'}

Started: {deployment.started_at or 'N/A'}
Completed: {deployment.completed_at or 'N/A'}
Duration: {deployment.duration or 0:.1f}s
"""

    if deployment.error_message:
        message += f"\nError: {deployment.error_message}\n"

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=False,
        )

        return {
            'success': True,
            'channel': 'email',
            'deployment_id': deployment_id,
            'recipients': recipients,
        }

    except Exception as e:
        logger.error(f"Email notification failed: {e}")
        return {
            'success': False,
            'error': str(e),
        }
