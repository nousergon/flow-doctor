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
import re
import sys
from typing import Any, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import (
    PREFLIGHT_UNREACHABLE_MARKER,
    Notifier,
    preflight_timeout,
)

_logger = logging.getLogger("flow_doctor")

# Telegram caps a single sendMessage payload at 4096 characters. The
# adapter truncates with a sentinel so the bot API never 400s on a
# long traceback / log capture.
_MAX_MESSAGE_LEN = 4096
_TRUNCATION_SUFFIX = "\n…[truncated]"
#: Used when the owning FlowDoctor did not describe its wiring.
DEFAULT_AUTO_FIX = (
    "Tonight's nightly sweep (7pm PT) root-causes this flow-doctor alert "
    "and opens the fix."
)
_AUTO_FIX_RE = re.compile(r"(?im)^[ \t]*[*_<b>]*auto-fix[*_]?[ \t]*:")
#: Used when no issue was filed for this alert (Brian, 2026-10-24: every
#: error and alert is paired with the issue or method that resolves it).
#: Never invents an issue number.
DEFAULT_RESOLUTION = (
    "none tracked yet. Tonight's nightly sweep (7pm PT) files an issue that "
    "lists the options and a recommended fix."
)
_RESOLUTION_RE = re.compile(r"(?im)^[ \t]*[*_<b>]*resolution[*_]?[ \t]*:")


def resolution_for_issue(issue_url: str) -> str:
    """The ``Resolution:`` text for an alert whose issue flow-doctor filed."""
    return (
        f"{issue_url} (filed by flow-doctor). If the issue has no plan yet, "
        "tonight's nightly sweep adds the options and a recommended fix."
    )

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
        # Plain-text "Auto-fix:" description; FlowDoctor sets it from its own
        # wiring (issue filing, remediation, fix PRs). Every alert carries the
        # line (observability-policy § Notification — auto-fix line).
        self.auto_fix: Optional[str] = None
        # Plain-text "Resolution:" — the issue that will end the problem.
        # FlowDoctor sets it per report from the GitHub issue it filed
        # (same policy section, Brian 2026-10-24); None renders the default.
        self.resolution: Optional[str] = None

    # ----- public API -----------------------------------------------------

    def _target_id(self) -> str:
        target = f"telegram:{self.chat_id}"
        if self.message_thread_id is not None:
            target += f":{self.message_thread_id}"
        return target

    def _deliver_text(
        self,
        text: str,
        *,
        parse_mode: Any = _UNSET,
        disable_notification: Any = _UNSET,
    ) -> bool:
        """POST text to Telegram; prefer krepis transport when installed."""
        mode = self.parse_mode if parse_mode is _UNSET else parse_mode
        quiet = (
            self.disable_notification
            if disable_notification is _UNSET
            else disable_notification
        )
        try:
            from krepis.telegram import send_message as krepis_send
        except ImportError:
            krepis_send = None  # type: ignore[assignment]

        if krepis_send is not None and mode in (None, "Markdown", "MarkdownV2"):
            # krepis handles Markdown v1 escape + optional forum topic routing.
            ok = krepis_send(
                text,
                disable_notification=bool(quiet),
                bot_token=self.bot_token,
                chat_id=self.chat_id,
                message_thread_id=self.message_thread_id,
            )
            if not ok:
                # krepis_send raises on error and only returns a plain bool
                # on the request path, so a False here carries no detail
                # beyond "the krepis transport reported failure."
                self.last_error = "krepis.telegram.send_message returned False"
            return ok

        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if mode:
            payload["parse_mode"] = mode
        if self.message_thread_id is not None:
            payload["message_thread_id"] = self.message_thread_id
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
                if resp.status != 200:
                    self.last_error = f"Telegram API returned HTTP {resp.status}"
                    _logger.critical(
                        "flow-doctor Telegram API returned HTTP %s", resp.status,
                    )
                    return False
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    self.last_error = "Telegram API returned non-JSON response"
                    return False
                if not parsed.get("ok"):
                    reason = parsed.get("description", "unknown")
                    self.last_error = f"Telegram API returned ok=false: {reason}"
                    _logger.critical(
                        "flow-doctor Telegram API returned ok=false: %s",
                        reason,
                    )
                    return False
                return True
        except URLError as e:
            self.last_error = f"{type(e).__name__}: {e}"
            _logger.critical(
                "flow-doctor Telegram notification failed (network): %s",
                e, exc_info=True,
            )
            print(
                f"[flow-doctor] Telegram notification failed: {e}",
                file=sys.stderr,
            )
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            _logger.critical(
                "flow-doctor Telegram notification failed: %s",
                e, exc_info=True,
            )
            print(
                f"[flow-doctor] Telegram notification failed: {e}",
                file=sys.stderr,
            )
            return False

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        self.last_error = None
        try:
            text = self._format_message(
                report, flow_name, diagnosis, auto_fix=self.auto_fix,
                resolution=self.resolution,
            )
            text = _truncate_keeping_auto_fix(text)
            if self._deliver_text(text):
                return self._target_id()
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
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
        if not _AUTO_FIX_RE.search(text):
            mode = self.parse_mode if parse_mode is _UNSET else parse_mode
            label = "*Auto-fix:*" if mode in ("Markdown", "MarkdownV2") else "Auto-fix:"
            text = f"{text}\n\n{label} {self.auto_fix or DEFAULT_AUTO_FIX}"
        if not _RESOLUTION_RE.search(text):
            mode = self.parse_mode if parse_mode is _UNSET else parse_mode
            label = "*Resolution:*" if mode in ("Markdown", "MarkdownV2") else "Resolution:"
            text = f"{text}\n{label} {self.resolution or DEFAULT_RESOLUTION}"
        text = _truncate_keeping_auto_fix(text) if "\n*Auto-fix:* " in text else _truncate(text)
        if self._deliver_text(
            text,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        ):
            return self._target_id()
        return None

    def validate(self) -> None:
        """Preflight: confirm the bot token is valid via ``getMe``.

        Raises :class:`ConfigError` only on a **verdict** — a reply in
        which Telegram itself rejected the credential (HTTP 401/403, or a
        200 carrying ``ok: false``). Every **transport** outcome — a read
        or connect timeout, DNS failure, connection reset, 5xx, 429 — is
        logged as a warning and does NOT block init.

        That split is the contract :class:`GitHubNotifier` and
        :class:`S3Notifier` already keep, and this notifier did not. Two
        defects followed from conflating them (alpha-engine-config-I8298):

        * ``TimeoutError`` is not a :class:`URLError` subclass, so a read
          timeout escaped the handler entirely and propagated out of
          ``FlowDoctor.__init__`` under ``strict``, killing the host
          process at import. On 2026-08-24 that took out the trading
          pipeline's market-hours gate and its deploy-drift gate on one
          unreachable ``api.telegram.org``.
        * :class:`HTTPError` IS a :class:`URLError` subclass, so a genuine
          401 was reported as "network" — the inverse error, from the same
          conflation.

        A monitoring channel that cannot be reached is a reason to warn,
        never a reason to stop the workload it was only there to watch.

        Bypassed entirely when ``FLOW_DOCTOR_SKIP_PREFLIGHT`` is set
        (mirrors the same env-var contract the other notifiers use for
        tests / offline boot).
        """
        import os

        if os.environ.get("FLOW_DOCTOR_SKIP_PREFLIGHT"):
            return None

        from flow_doctor.core.errors import ConfigError

        try:
            req = Request(
                f"{self._API_BASE}/bot{self.bot_token}/getMe",
                method="GET",
            )
            with urlopen(req, timeout=preflight_timeout()) as resp:
                if resp.status != 200:
                    # urlopen raises HTTPError for >=400, so anything that
                    # lands here is a 2xx/3xx anomaly, not a credential
                    # verdict. Report it; do not block startup on it.
                    _logger.warning(
                        "flow-doctor Telegram preflight returned HTTP %s "
                        "(not an auth verdict, proceeding)",
                        resp.status,
                    )
                    return None
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    # A non-JSON 200 is a captive portal / proxy
                    # interstitial, not Telegram answering about the token.
                    _logger.warning(
                        "flow-doctor Telegram preflight returned non-JSON "
                        "(not an auth verdict, proceeding)",
                    )
                    return None
                if not parsed.get("ok"):
                    raise ConfigError(
                        "Telegram bot token preflight failed: "
                        f"{parsed.get('description', 'unknown error')}. "
                        "Verify the token at https://t.me/BotFather (/mybots -> API Token)."
                    )
        except HTTPError as e:
            # Telegram ANSWERED. 401/403 is a verdict on the credential;
            # anything else it returns is its problem, not our config.
            if e.code in (401, 403):
                raise ConfigError(
                    f"Telegram bot token rejected by api.telegram.org (HTTP {e.code}). "
                    "Verify the token at https://t.me/BotFather (/mybots -> API Token)."
                ) from e
            _logger.warning(
                "flow-doctor Telegram preflight returned HTTP %s "
                "(not an auth verdict, proceeding): %s",
                e.code, e,
            )
        except (URLError, TimeoutError, OSError) as e:
            # Transport. `TimeoutError` is listed explicitly because it is
            # NOT a URLError subclass — it is exactly what a read timeout
            # against a slow api.telegram.org raises, and it is what
            # crashed the predictor Lambda at import on 2026-08-24.
            _logger.warning(
                "%s flow-doctor Telegram preflight unreachable (proceeding, "
                "token unverified): %s: %s",
                PREFLIGHT_UNREACHABLE_MARKER, type(e).__name__, e,
            )

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _format_message(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
        auto_fix: Optional[str] = None,
        resolution: Optional[str] = None,
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

        if report.logs:
            log_lines = report.logs.strip().splitlines()[-20:]
            lines.append("")
            lines.append("```")
            lines.extend(log_lines)
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
        elif report.diagnosis_error:
            lines.append("")
            lines.append(f"*Diagnosis:* ⚠️ unavailable")
            lines.append(f"_{report.diagnosis_error[:300]}_")

        lines.append(f"\n_Report ID: {report.id}_")
        lines.append(f"\n*Auto-fix:* {auto_fix or DEFAULT_AUTO_FIX}")
        lines.append(f"*Resolution:* {resolution or DEFAULT_RESOLUTION}")
        return "\n".join(lines)


def _truncate_keeping_auto_fix(text: str) -> str:
    """Truncate a formatted alert but keep its ``Auto-fix:``/``Resolution:`` footer."""
    if len(text) <= _MAX_MESSAGE_LEN:
        return text
    head, sep, fix = text.rpartition("\n*Auto-fix:* ")
    if not sep:
        return _truncate(text)
    tail = sep + fix
    keep = max(_MAX_MESSAGE_LEN - len(tail) - len(_TRUNCATION_SUFFIX), 0)
    return head[:keep] + _TRUNCATION_SUFFIX + tail


def _truncate(text: str) -> str:
    if len(text) <= _MAX_MESSAGE_LEN:
        return text
    keep = _MAX_MESSAGE_LEN - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


__all__ = ["TelegramNotifier"]
