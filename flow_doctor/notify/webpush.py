"""Web Push (VAPID) notification backend.

Unlike Telegram/Slack/email, Web Push has no static "address" a notifier
can be configured with once and reuse forever — it needs a *subscription*
object (``{"endpoint": ..., "keys": {...}}``) minted by a specific
browser/device the moment a human grants notification permission on some
page. flow-doctor itself has no such page (it runs in Lambdas/EC2/cron,
never a browser), so provisioning a subscription is out-of-band: visit
whatever site owns the subscribe flow (e.g. symposion), grant permission,
and paste the resulting ``PushSubscription.toJSON()`` into
``FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION`` (or a ``WebPushNotifierConfig(
subscription=...)`` inline). One subscription = one device; register the
same flow's alerts to more than one device by adding more than one
``WebPushNotifierConfig`` to ``notify``.

Unlike Telegram's soft/optional krepis dependency (there's a legit raw-
``urlopen`` HTTP fallback), Web Push has no reasonable non-krepis fallback
— the actual send is VAPID JWT signing + AES128GCM payload encryption, not
a REST call flow-doctor should reimplement. ``krepis[webpush]`` is
therefore a REQUIRED dependency for this notifier specifically: missing it
raises ``ConfigError`` at ``FlowDoctor.__init__`` time (fail-loud, same as
a missing bot_token), not a silent per-send no-op.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier

_logger = logging.getLogger("flow_doctor")

_SEVERITY_EMOJI = {"critical": "🔴", "error": "🟠", "warning": "🟡"}
_MAX_BODY_LEN = 300  # most push services truncate the *displayed* body well
# below this; keep the payload itself compact rather than relying on the
# OS/browser to do it.


class WebPushNotifier(Notifier):
    """Send alerts via Web Push (VAPID) to one subscribed device.

    :param subscription: The W3C ``PushSubscription.toJSON()`` object.
    :param url: Optional URL the client's ``notificationclick`` handler
        should open/focus (e.g. a dashboard link).
    :param vapid_private_key: Optional explicit VAPID private key —
        when omitted, ``krepis.webpush.send_push`` resolves the fleet's
        shared identity via ``krepis.secrets.get_secret``.
    :param vapid_subject: Optional explicit VAPID JWT ``sub`` claim,
        same fallback-to-krepis-default behavior as ``vapid_private_key``.
    """

    def __init__(
        self,
        subscription: Dict[str, Any],
        *,
        url: Optional[str] = None,
        vapid_private_key: Optional[str] = None,
        vapid_subject: Optional[str] = None,
    ):
        self.subscription = subscription
        self.url = url
        self.vapid_private_key = vapid_private_key
        self.vapid_subject = vapid_subject

    def _target_id(self) -> str:
        # The full endpoint URL is a bearer-capability (anyone holding it
        # can push to this device) - never let it land in the actions
        # table's target field. The push service host is enough to tell
        # notifiers apart operationally (e.g. "fcm.googleapis.com" vs
        # "web.push.apple.com").
        host = urlparse(self.subscription.get("endpoint", "")).netloc
        return f"webpush:{host or 'unknown'}"

    @staticmethod
    def _format_title(report: Report, flow_name: str) -> str:
        emoji = _SEVERITY_EMOJI.get(report.severity, "⚪")
        return f"{emoji} [{report.severity.upper()}] {flow_name}"

    @staticmethod
    def _format_body(report: Report, diagnosis: Optional[Diagnosis] = None) -> str:
        if report.error_type:
            body = f"{report.error_type}: {report.error_message}"
        else:
            body = report.error_message
        if diagnosis:
            body = f"{body}\n{diagnosis.category}: {diagnosis.root_cause[:150]}"
        if len(body) > _MAX_BODY_LEN:
            body = body[: _MAX_BODY_LEN - 1] + "…"
        return body

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        try:
            from krepis.webpush import send_push
        except ImportError:
            _logger.critical(
                "flow-doctor Web Push notification failed: krepis[webpush] "
                "is not installed (pip install 'flow-doctor[webpush]')"
            )
            return None
        try:
            ok = send_push(
                self.subscription,
                title=self._format_title(report, flow_name),
                body=self._format_body(report, diagnosis),
                url=self.url,
                tag=flow_name,
                vapid_private_key=self.vapid_private_key,
                vapid_subject=self.vapid_subject,
            )
        except Exception as e:
            _logger.critical(
                "flow-doctor Web Push notification failed: %s", e, exc_info=True,
            )
            print(f"[flow-doctor] Web Push notification failed: {e}", file=sys.stderr)
            return None
        return self._target_id() if ok else None


__all__ = ["WebPushNotifier"]
