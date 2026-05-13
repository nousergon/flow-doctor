"""Remediation executor: runs auto-fix actions (SSM, Step Functions, restarts).

Supports dry-run mode where actions are logged but not executed.
All actions are persisted to the SQLite audit trail.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from flow_doctor.remediation.decision_gate import Decision, DecisionType
from flow_doctor.remediation.playbook import RemediationAction, RemediationType

if TYPE_CHECKING:
    from flow_doctor.notify.telegram import TelegramNotifier

logger = logging.getLogger("flow_doctor.remediation")


@dataclass
class ExecutionResult:
    """Result of a remediation execution."""
    success: bool
    action_type: str
    commands_run: List[str] = field(default_factory=list)
    output: str = ""
    error: str = ""
    dry_run: bool = False


class RemediationExecutor:
    """Executes remediation actions from decision gate decisions.

    Supports:
    - SSM RunCommand for EC2 instances
    - Step Function StartExecution
    - EC2 start/stop instances
    - Dry-run mode (log only)
    """

    def __init__(
        self,
        dry_run: bool = True,
        ssm_client=None,
        sfn_client=None,
        ec2_client=None,
        store=None,
        telegram_notifier: "Optional[TelegramNotifier]" = None,
        telegram_webhook_url: Optional[str] = None,
    ):
        """
        Args:
            telegram_notifier: First-class ``TelegramNotifier`` to route
                remediation pings through (preferred since 0.5.0rc3).
                When supplied, gets bot-token / chat-id / threading /
                Markdown formatting / target-id auditing.
            telegram_webhook_url: Legacy back-compat path — POSTs a
                ``{"text": ...}`` body to an arbitrary URL via urllib.
                Kept so 0.4.x ``flow-doctor.yaml`` configs keep working
                without code changes; will be removed in 0.6.0.
        """
        self.dry_run = dry_run
        self._ssm = ssm_client
        self._sfn = sfn_client
        self._ec2 = ec2_client
        self._store = store
        self._telegram_notifier = telegram_notifier
        self._telegram_url = telegram_webhook_url

    def execute(self, decision: Decision) -> ExecutionResult:
        """Execute a remediation decision. Returns ExecutionResult."""
        if decision.decision_type != DecisionType.AUTO_REMEDIATE:
            return ExecutionResult(
                success=False,
                action_type="none",
                error=f"Decision type {decision.decision_type} is not auto-remediate",
            )

        action = decision.action
        if not action:
            return ExecutionResult(
                success=False,
                action_type="none",
                error="No remediation action in decision",
            )

        result = self._dispatch(action, decision)

        # Persist to audit trail
        self._save_audit(decision, result)

        # Send Telegram notification
        self._notify_telegram(decision, result)

        return result

    def _dispatch(self, action: RemediationAction, decision: Decision) -> ExecutionResult:
        """Route to the appropriate executor based on action type."""
        if action.action_type == RemediationType.RESTART_SERVICE:
            return self._execute_ssm(action, decision)
        elif action.action_type == RemediationType.RERUN_STEP:
            return self._execute_rerun(action, decision)
        elif action.action_type == RemediationType.UPDATE_CONFIG:
            return self._execute_config_update(action, decision)
        else:
            return ExecutionResult(
                success=False,
                action_type=action.action_type.value,
                error=f"Unsupported action type for auto-execution: {action.action_type}",
            )

    def _execute_ssm(self, action: RemediationAction, decision: Decision) -> ExecutionResult:
        """Execute commands on EC2 via SSM RunCommand."""
        if not action.commands:
            return ExecutionResult(
                success=False, action_type="restart_service",
                error="No commands to execute",
            )

        if self.dry_run:
            logger.info("[DRY RUN] Would execute on %s: %s",
                        action.ssm_target, action.commands)
            return ExecutionResult(
                success=True,
                action_type="restart_service",
                commands_run=action.commands,
                output=f"[DRY RUN] {len(action.commands)} commands on {action.ssm_target}",
                dry_run=True,
            )

        if not self._ssm:
            return ExecutionResult(
                success=False, action_type="restart_service",
                error="SSM client not configured",
            )

        try:
            # Build the SSM command
            command_str = " && ".join(action.commands)
            instance_ids = self._resolve_instance_ids(action.ssm_target)

            if not instance_ids:
                return ExecutionResult(
                    success=False, action_type="restart_service",
                    error=f"No instances found for target: {action.ssm_target}",
                )

            resp = self._ssm.send_command(
                InstanceIds=instance_ids,
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [command_str]},
                TimeoutSeconds=120,
                Comment=f"flow-doctor remediation: {decision.playbook_match.name if decision.playbook_match else 'unknown'}",
            )

            command_id = resp["Command"]["CommandId"]
            logger.info("SSM command sent: %s -> %s (cmd: %s)",
                        action.ssm_target, command_str[:100], command_id)

            return ExecutionResult(
                success=True,
                action_type="restart_service",
                commands_run=action.commands,
                output=f"SSM command {command_id} sent to {instance_ids}",
            )

        except Exception as e:
            logger.error("SSM execution failed: %s", e)
            return ExecutionResult(
                success=False, action_type="restart_service",
                commands_run=action.commands,
                error=str(e),
            )

    def _execute_rerun(self, action: RemediationAction, decision: Decision) -> ExecutionResult:
        """Re-trigger a Step Function execution or Lambda."""
        if self.dry_run:
            logger.info("[DRY RUN] Would rerun step for: %s",
                        decision.diagnosis.flow_name)
            return ExecutionResult(
                success=True,
                action_type="rerun_step",
                output=f"[DRY RUN] Would rerun {decision.diagnosis.flow_name}",
                dry_run=True,
            )

        if action.step_function_arn and self._sfn:
            try:
                sfn_input = action.step_function_input or {}
                resp = self._sfn.start_execution(
                    stateMachineArn=action.step_function_arn,
                    input=json.dumps(sfn_input),
                )
                execution_arn = resp["executionArn"]
                logger.info("Step Function started: %s", execution_arn)
                return ExecutionResult(
                    success=True,
                    action_type="rerun_step",
                    output=f"Started execution: {execution_arn}",
                )
            except Exception as e:
                logger.error("Step Function start failed: %s", e)
                return ExecutionResult(
                    success=False, action_type="rerun_step", error=str(e),
                )

        return ExecutionResult(
            success=False, action_type="rerun_step",
            error="No Step Function ARN configured or SFN client not available",
        )

    def _execute_config_update(self, action: RemediationAction, decision: Decision) -> ExecutionResult:
        """Config updates always escalate — too risky for auto-execution."""
        return ExecutionResult(
            success=False,
            action_type="update_config",
            error="Config updates require human review — escalating",
        )

    def _resolve_instance_ids(self, target: Optional[str]) -> List[str]:
        """Resolve an SSM target name to EC2 instance IDs."""
        if not target or not self._ec2:
            return []

        try:
            resp = self._ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": [target]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ],
            )
            ids = []
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    ids.append(inst["InstanceId"])
            return ids
        except Exception as e:
            logger.error("Failed to resolve instance IDs for %s: %s", target, e)
            return []

    def _save_audit(self, decision: Decision, result: ExecutionResult) -> None:
        """Persist remediation action to the audit trail."""
        if not self._store:
            return

        try:
            self._store.save_remediation_action(
                report_id=decision.diagnosis.report_id,
                diagnosis_id=decision.diagnosis.id,
                decision_type=decision.decision_type.value,
                playbook_pattern=decision.playbook_match.name if decision.playbook_match else None,
                action_type=result.action_type,
                commands=result.commands_run or None,
                dry_run=result.dry_run,
                success=result.success,
                output=result.output[:2000] if result.output else None,
                error=result.error[:2000] if result.error else None,
            )
        except Exception as e:
            logger.error("Failed to save remediation audit: %s", e)

    def _notify_telegram(self, decision: Decision, result: ExecutionResult) -> None:
        """Send Telegram notification for every remediation action.

        Preferred path (0.5.0rc3+): a first-class ``TelegramNotifier``
        passed via ``telegram_notifier=``. The pre-rc3 ``telegram_webhook_url``
        path is kept for back-compat with 0.4.x yaml configs and will be
        removed in 0.6.0.
        """
        if not self._telegram_notifier and not self._telegram_url:
            return

        msg = self._format_remediation_message(decision, result)

        # Prefer the first-class notifier when configured.
        if self._telegram_notifier is not None:
            try:
                self._telegram_notifier.send_raw(msg)
            except Exception as e:
                # send_raw() already logs + swallows; this except is the
                # belt-and-suspenders barrier for anything that slips
                # past, since the executor must never crash on
                # notification failure.
                logger.warning("Telegram notifier failed: %s", e)
            return

        # Legacy webhook-URL path.
        try:
            import urllib.request
            data = json.dumps({"text": msg}).encode("utf-8")
            req = urllib.request.Request(
                self._telegram_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning("Telegram notification failed: %s", e)

    @staticmethod
    def _format_remediation_message(
        decision: Decision, result: ExecutionResult
    ) -> str:
        emoji = "✅" if result.success else "❌"
        mode = "[DRY RUN] " if result.dry_run else ""
        pattern = (
            decision.playbook_match.name
            if decision.playbook_match
            else "unknown"
        )
        msg = (
            f"{emoji} {mode}flow-doctor auto-remediation\n"
            f"Pattern: {pattern}\n"
            f"Action: {result.action_type}\n"
            f"Flow: {decision.diagnosis.flow_name}\n"
            f"Root cause: {decision.diagnosis.root_cause[:200]}\n"
        )
        if result.error:
            msg += f"Error: {result.error[:200]}\n"
        return msg
