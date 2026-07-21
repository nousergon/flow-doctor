"""Slack webhook notification backend."""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier

_logger = logging.getLogger("flow_doctor")


class SlackNotifier(Notifier):
    """Send alerts via Slack incoming webhook."""

    def __init__(self, webhook_url: str, channel: Optional[str] = None):
        self.webhook_url = webhook_url
        self.channel = channel

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        try:
            text = self._format_message(report, flow_name, diagnosis)
            payload = {"text": text}
            if self.channel:
                payload["channel"] = self.channel

            data = json.dumps(payload).encode("utf-8")
            req = Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    # Return channel (if set) or a generic "slack" target.
                    # We don't return the full webhook_url because it's
                    # a secret that shouldn't be persisted to the DB.
                    return self.channel or "slack"
                _logger.critical(
                    "flow-doctor Slack webhook returned HTTP %s", resp.status,
                )
                return None
        except Exception as e:
            _logger.critical(
                "flow-doctor Slack notification failed: %s", e, exc_info=True,
            )
            print(f"[flow-doctor] Slack notification failed: {e}", file=sys.stderr)
            return None

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
            f"{emoji} *[{report.severity.upper()}] {flow_name}*",
            "",
        ]
        if report.error_type:
            lines.append(f"*Error:* `{report.error_type}: {report.error_message}`")
        else:
            lines.append(f"*Message:* {report.error_message}")

        if report.cascade_source:
            lines.append(f"_Likely caused by upstream `{report.cascade_source}` failure_")

        if report.traceback:
            # Show last 5 lines of traceback
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

        # Diagnosis enrichment
        if diagnosis:
            category_emoji = {
                "TRANSIENT": "🔄", "DATA": "📊", "CODE": "🐛",
                "CONFIG": "⚙️", "EXTERNAL": "🌐", "INFRA": "🏗️",
            }.get(diagnosis.category, "❓")

            lines.append("")
            lines.append(f"*Diagnosis:* {category_emoji} {diagnosis.category} (confidence: {diagnosis.confidence:.0%})")
            lines.append(f"_{diagnosis.root_cause[:300]}_")

            if diagnosis.remediation:
                lines.append(f"\n*Remediation:* {diagnosis.remediation[:300]}")

        lines.append(f"\n_Report ID: {report.id}_")
        return "\n".join(lines)
