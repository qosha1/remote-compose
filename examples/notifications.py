"""
Notifications and webhooks example.

This example demonstrates:
- Sending webhook notifications
- Slack integration
- Email notifications
- Configuring notification channels
"""

from remote_compose.tasks import (
    send_webhook,
    send_deployment_notification,
    send_slack_notification,
    send_email_notification,
)
from remote_compose.models import Deployment


def send_webhook_notification():
    """Send a webhook notification for a deployment event."""
    # Send webhook asynchronously
    result = send_webhook.delay(
        webhook_url='https://your-server.com/api/webhooks/deployment',
        event='deployment.completed',
        payload={
            'deployment_id': 123,
            'project_name': 'myapp',
            'version': 'v2.0.0',
            'status': 'success',
            'target': 'prod-server-1',
        },
        # Optional custom headers
        headers={
            'X-Custom-Header': 'value',
            'Authorization': 'Bearer your-token',
        },
        timeout=30,
    )

    print(f"Webhook task ID: {result.id}")

    # Wait for result (optional)
    webhook_result = result.get(timeout=60)
    print(f"Webhook result: {webhook_result}")


def send_full_deployment_notification(deployment_id):
    """Send notification to all configured channels."""
    result = send_deployment_notification.delay(
        deployment_id=deployment_id,
        event='deployment.completed',
        # Optional: specify channels (default: all configured)
        channels=['webhook', 'slack'],
        # Optional: extra data to include
        extra_data={
            'release_notes': 'Bug fixes and performance improvements',
            'jira_ticket': 'PROJ-1234',
        },
    )

    notification_result = result.get(timeout=60)
    print(f"Notifications sent: {notification_result['notifications_sent']}")

    for r in notification_result['results']:
        print(f"  - {r['channel']}: task {r['task_id']}")


def send_slack_notification_example():
    """Send a Slack notification."""
    result = send_slack_notification.delay(
        deployment_id=123,
        event='deployment.completed',
        payload={
            'deployment_id': 123,
            'project_name': 'myapp',
            'version': 'v2.0.0',
            'status': 'success',
            'target': {'name': 'prod-server-1'},
            'deployed_by': 'admin@example.com',
            'duration_seconds': 45.2,
        },
    )

    slack_result = result.get(timeout=60)
    print(f"Slack notification result: {slack_result}")


def send_email_notification_example():
    """Send email notification for a deployment."""
    result = send_email_notification.delay(
        deployment_id=123,
        event='deployment.completed',
        recipients=[
            'team@example.com',
            'devops@example.com',
        ],
    )

    email_result = result.get(timeout=60)
    print(f"Email notification result: {email_result}")


def configure_notification_channels():
    """
    Configure notification channels in Django settings.

    Add these to your Django settings REMOTE_COMPOSE dict.
    """
    notification_config = {
        # Enable notification channels
        'NOTIFICATION_CHANNELS': ['webhook', 'slack', 'email'],

        # Webhook configuration
        'NOTIFICATION_WEBHOOK_URLS': [
            'https://app1.example.com/webhooks/deploy',
            'https://app2.example.com/webhooks/deploy',
        ],

        # Security: Only allow webhooks to specific domains
        'WEBHOOK_ALLOWED_DOMAINS': {
            'localhost',
            'app1.example.com',
            'app2.example.com',
            'hooks.slack.com',
        },
        # Or allow all domains (not recommended for production)
        'WEBHOOK_ALLOW_ALL_DOMAINS': False,

        # Slack configuration
        'SLACK_WEBHOOK_URL': 'https://hooks.slack.com/services/T00/B00/XXXX',

        # Email is configured via Django's email settings:
        # EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, etc.
    }

    print("Notification Configuration:")
    for key, value in notification_config.items():
        print(f"  {key}: {value}")

    return notification_config


def webhook_payload_format():
    """
    Webhook payload format documentation.

    Webhooks are sent as POST requests with JSON body.
    """
    example_payload = {
        # Event type
        "event": "deployment.completed",

        # ISO 8601 timestamp
        "timestamp": "2024-01-15T10:30:00+00:00",

        # Event data
        "data": {
            "deployment_id": 123,
            "project_name": "myapp",
            "version": "v2.0.0",
            "status": "success",
            "target": {
                "id": 1,
                "name": "prod-server-1",
                "host": "192.168.1.100",
            },
            "deployed_by": "admin@example.com",
            "started_at": "2024-01-15T10:29:00+00:00",
            "completed_at": "2024-01-15T10:30:00+00:00",
            "duration_seconds": 45.2,
            "error_message": None,  # or error string if failed
        }
    }

    print("Webhook Payload Format:")
    import json
    print(json.dumps(example_payload, indent=2))

    return example_payload


def webhook_events():
    """
    List of webhook event types.

    Configure your webhook receiver to handle these events.
    """
    events = [
        # Deployment events
        'deployment.started',    # Deployment has begun
        'deployment.completed',  # Deployment finished successfully
        'deployment.failed',     # Deployment failed

        # Rollback events
        'rollback.started',      # Rollback has begun
        'rollback.completed',    # Rollback finished successfully
        'rollback.failed',       # Rollback failed

        # Health events
        'health.check.failed',   # Health check failed
        'health.target.unhealthy',  # Target became unhealthy

        # Custom events (you can add your own)
        'custom.event.name',
    ]

    print("Available Webhook Events:")
    for event in events:
        print(f"  - {event}")

    return events


def setup_webhook_receiver():
    """
    Example webhook receiver (Flask app).

    This shows how to receive and process webhooks.
    """
    code = '''
# Example Flask webhook receiver

from flask import Flask, request, jsonify
import hmac
import hashlib

app = Flask(__name__)

# Optional: Verify webhook signature
WEBHOOK_SECRET = 'your-webhook-secret'

def verify_signature(payload, signature):
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)

@app.route('/webhooks/deployment', methods=['POST'])
def handle_deployment_webhook():
    # Get the payload
    payload = request.json

    # Optional: Verify signature
    signature = request.headers.get('X-Webhook-Signature', '')
    if WEBHOOK_SECRET and not verify_signature(request.data, signature):
        return jsonify({'error': 'Invalid signature'}), 401

    # Process the event
    event = payload.get('event')
    data = payload.get('data', {})

    print(f"Received event: {event}")
    print(f"Deployment: {data.get('project_name')} -> {data.get('status')}")

    # Handle different events
    if event == 'deployment.completed':
        # Notify team, update dashboard, etc.
        pass
    elif event == 'deployment.failed':
        # Alert on-call, create incident, etc.
        pass

    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(port=5000)
'''

    print("Example Webhook Receiver (Flask):")
    print(code)


if __name__ == '__main__':
    print("=" * 50)
    print("Notification Configuration")
    print("=" * 50)
    configure_notification_channels()

    print("\n" + "=" * 50)
    print("Webhook Events")
    print("=" * 50)
    webhook_events()

    print("\n" + "=" * 50)
    print("Webhook Payload Format")
    print("=" * 50)
    webhook_payload_format()
