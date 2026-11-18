"""Helper for writing activity-log entries from views."""
import logging

from .models import AuditEvent

logger = logging.getLogger(__name__)


def _client_ip(request):
    # REMOTE_ADDR only: X-Forwarded-For is client-controlled unless a trusted proxy
    # rewrites it. Behind a reverse proxy, configure it to set REMOTE_ADDR (e.g. via
    # gunicorn --forwarded-allow-ips) rather than trusting the header here.
    return request.META.get("REMOTE_ADDR") or None


def log_event(request, action, *, actor=None, actor_username="", target=None, target_type="", target_id="", **detail):
    """
    Record an activity-log entry. `actor` defaults to the authenticated request user.
    `target` may be a model instance (its class name and pk are stored).

    Never raises: failing to write an audit row must not break the user's action,
    but it is logged loudly so it can be investigated.
    """
    try:
        if actor is None and getattr(request, "user", None) is not None and request.user.is_authenticated:
            actor = request.user
        if target is not None:
            target_type = target_type or target.__class__.__name__
            target_id = target_id or str(target.pk)
        AuditEvent.objects.create(
            action=action,
            actor=actor,
            actor_username=(actor.get_username() if actor is not None else actor_username)[:150],
            target_type=target_type[:50],
            target_id=str(target_id)[:64],
            ip_address=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write audit event %s", action)
