"""Telegram Bot API notification backend.

Telegram is the recommended default for flow-doctor consumers — one bot
token gets you N routed channels via ``chat_id`` and optional forum
``message_thread_id``, mobile push is automatic, and credential rotation
is one ``@BotFather`` call. SMTP/SES email + Slack + GitHub stay as
alternates for consumers that need them.

Setup recipe::

    1. Message @BotFather → /newbot → save the bot token.
    2. Add the bot to your target chat / channel.
    3. Get the chat_id:
       - Personal chat: send a message to the bot, then
         GET https://api.telegram.org/bot<TOKEN>/getUpdates and read
         result[].message.chat.id (positive integer).
       - Group / channel: as above (negative integer, often starts with
         -100 for supergroups + channels).
       - Forum-style supergroup: also note message_thread_id for the
         specific topic you want notifications routed to.
    4. Set FLOW_DOCTOR_TELEGRAM_BOT_TOKEN + FLOW_DOCTOR_TELEGRAM_CHAT_ID
       in the env, or pass them inline via TelegramNotifierConfig.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional, Union
from urllib.error import URLError
from urllib.request import Request, urlopen

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier

_logger = logging.getLogger("flow_doctor")

# Telegram caps a single sendMessage payload at 4096 characters. The
# adapter truncates with a sentinel so the bot API never 400s on a
# long traceback / log capture.
_MAX_MESSAGE_LEN = 4096
_TRUNCATION_SUFFIX = "\n…[truncated]"

# Sentinel for ``send_raw`` overrides — lets us distinguish "caller
# didn't pass this kwarg, use instance default" from "caller explicitly
# passed None to override to plain text / push-with-sound".
_UNSET: Any = object()


class TelegramNotifier(Notifier):
    """Send alerts via the Telegram Bot API.

    ``chat_id`` may be an integer (typical) or a ``@channelusername``
    string (public channels only). ``message_thread_id`` routes the
    message to a specific topic in a forum-style supergroup, which is
    the cleanest way to fan out N flow-doctor flows into one chat
    without N bots.
    """

    _API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str,
        chat_id: Union[int, str],
        *,
        message_thread_id: Optional[int] = None,
        parse_mode: Optional[str] = "Markdown",
        disable_notification: bool = False,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.parse_mode = parse_mode
        self.disable_notification = disable_notification

    # ----- public API -----------------------------------------------------

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        try:
            text = self._format_message(report, flow_name, diagnosis)
            text = _truncate(text)
            payload = {
                "chat_id": self.chat_id,
                "text": text,
            }
            if self.parse_mode:
                payload["parse_mode"] = self.parse_mode
            if self.message_thread_id is not None:
                payload["message_thread_id"] = self.message_thread_id
            if self.disable_notification:
                payload["disable_notification"] = True

            data = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{self._API_BASE}/bot{self.bot_token}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {}
                    if parsed.get("ok"):
                        # Return a stable, non-secret target identifier so
                        # the action target can be stored without leaking
                        # the bot token. ``chat_id`` is enough to identify
                        # the destination on a per-install basis.
                        target = f"telegram:{self.chat_id}"
                        if self.message_thread_id is not None:
                            target += f":{self.message_thread_id}"
                        return target
                    description = parsed.get("description", "unknown")
                    _logger.critical(
                        "flow-doctor Telegram API returned ok=false: %s",
                        description,
                    )
                    return None
                _logger.critical(
                    "flow-doctor Telegram API returned HTTP %s", resp.status,
                )
                return None
        except URLError as e:
            _logger.critical(
                "flow-doctor Telegram notification failed (network): %s",
                e, exc_info=True,
            )
            print(
                f"[flow-doctor] Telegram notification failed: {e}",
                file=sys.stderr,
            )
            return None
        except Exception as e:
            _logger.critical(
                "flow-doctor Telegram notification failed: %s",
                e, exc_info=True,
            )
            print(
                f"[flow-doctor] Telegram notification failed: {e}",
                file=sys.stderr,
            )
            return None

    def send_raw(
        self,
        text: str,
        *,
        parse_mode: Any = _UNSET,
        disable_notification: Any = _UNSET,
    ) -> Optional[str]:
        """POST an arbitrary text message to the configured chat.

        Distinct from :meth:`send`, which formats a structured Report.
        ``send_raw`` is the convenience for adjacent flow-doctor
        subsystems (remediation, custom success pings) that want to ride
        the same bot + chat + thread routing without conforming to the
        Report shape. Returns the standard non-secret target identifier
        on success, or None on failure (errors are logged, never raised).

        ``parse_mode`` and ``disable_notification`` default to the
        instance values supplied at construction time. Explicit
        overrides — including ``parse_mode=None`` for plain-text
        rendering when the body contains characters that Markdown
        would otherwise mangle — are honoured. The sentinel lets us
        distinguish "use instance default" from "explicit None".
        """
        text = _truncate(text)
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
        }
        mode = self.parse_mode if parse_mode is _UNSET else parse_mode
        if mode:
            payload["parse_mode"] = mode
        if self.message_thread_id is not None:
            payload["message_thread_id"] = self.message_thread_id
        quiet = (
            self.disable_notification
            if disable_notification is _UNSET
            else disable_notification
        )
        if quiet:
            payload["disable_notification"] = True

        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{self._API_BASE}/bot{self.bot_token}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {}
                    if parsed.get("ok"):
                        target = f"telegram:{self.chat_id}"
                        if self.message_thread_id is not None:
                            target += f":{self.message_thread_id}"
                        return target
                    _logger.critical(
                        "flow-doctor Telegram send_raw ok=false: %s",
                        parsed.get("description", "unknown"),
                    )
                    return None
                _logger.critical(
                    "flow-doctor Telegram send_raw HTTP %s", resp.status,
                )
                return None
        except Exception as e:
            _logger.warning(
                "flow-doctor Telegram send_raw failed: %s", e,
            )
            return None

    def validate(self) -> None:
        """Preflight: confirm the bot token is valid via ``getMe``.

        Bypassed when ``FLOW_DOCTOR_SKIP_PREFLIGHT`` is set (mirrors the
        same env-var contract the other notifiers use for tests / offline
        boot)."""
        import os

        if os.environ.get("FLOW_DOCTOR_SKIP_PREFLIGHT"):
            return None
        try:
            req = Request(
                f"{self._API_BASE}/bot{self.bot_token}/getMe",
                method="GET",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    from flow_doctor.core.errors import ConfigError

                    raise ConfigError(
                        f"Telegram bot token preflight failed: HTTP {resp.status}. "
                        "Verify the token at https://t.me/BotFather (/mybots → API Token)."
                    )
                body = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                if not parsed.get("ok"):
                    from flow_doctor.core.errors import ConfigError

                    raise ConfigError(
                        "Telegram bot token preflight failed: "
                        f"{parsed.get('description', 'unknown error')}. "
                        "Verify the token at https://t.me/BotFather (/mybots → API Token)."
                    )
        except URLError as e:
            from flow_doctor.core.errors import ConfigError

            raise ConfigError(
                f"Telegram bot token preflight failed (network): {e}. "
                "Check connectivity to api.telegram.org or set "
                "FLOW_DOCTOR_SKIP_PREFLIGHT=1 to defer the check."
            )

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _format_message(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> str:
        severity_emoji = {
            "critical": "🔴",
            "error": "🟠",
            "warning": "🟡",
        }
        emoji = severity_emoji.get(report.severity, "⚪")
        lines = [
            f"{emoji} *\\[{report.severity.upper()}\\] {flow_name}*",
            "",
        ]
        if report.error_type:
            lines.append(f"*Error:* `{report.error_type}: {report.error_message}`")
        else:
            lines.append(f"*Message:* {report.error_message}")

        if report.cascade_source:
            lines.append(
                f"_Likely caused by upstream `{report.cascade_source}` failure_"
            )

        if report.traceback:
            tb_lines = report.traceback.strip().splitlines()[-5:]
            lines.append("")
            lines.append("```")
            lines.extend(tb_lines)
            lines.append("```")

        if diagnosis:
            category_emoji = {
                "TRANSIENT": "🔄", "DATA": "📊", "CODE": "🐛",
                "CONFIG": "⚙️", "EXTERNAL": "🌐", "INFRA": "🏗️",
            }.get(diagnosis.category, "❓")

            lines.append("")
            lines.append(
                f"*Diagnosis:* {category_emoji} {diagnosis.category} "
                f"(confidence: {diagnosis.confidence:.0%})"
            )
            lines.append(f"_{diagnosis.root_cause[:300]}_")

            if diagnosis.remediation:
                lines.append(f"\n*Remediation:* {diagnosis.remediation[:300]}")

        lines.append(f"\n_Report ID: {report.id}_")
        return "\n".join(lines)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_MESSAGE_LEN:
        return text
    keep = _MAX_MESSAGE_LEN - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


__all__ = ["TelegramNotifier"]
