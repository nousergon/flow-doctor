"""DynamoDB storage backend for cross-invocation dedup + persistence.

Fleet Lambdas and short-lived runners share one DynamoDB table so dedup
cooldowns and rate-limit counters survive across invocations (SQLite in
``/tmp`` cannot).

Table schema (single-table, provisioned by ``init_schema`` in dev/moto;
production table should mirror this via IaC):

- PK ``pk``, SK ``sk`` (strings)
- GSI ``SignatureIndex``: ``error_signature`` (HASH), ``created_at`` (RANGE)
- GSI ``FlowIndex``: ``flow_name`` (HASH), ``created_at`` (RANGE)
- GSI ``ActionTypeIndex``: ``action_type`` (HASH), ``created_at`` (RANGE)

Each entity is stored as a JSON ``payload`` plus denormalized index keys.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from flow_doctor.core.models import (
    Action,
    Decision,
    Diagnosis,
    FixAttempt,
    KnownPattern,
    Report,
)
from flow_doctor.storage.base import StorageBackend

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - optional at import, required at runtime
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[misc, assignment]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _report_to_item(report: Report) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "pk": f"REPORT#{report.id}",
        "sk": "META",
        "entity": "report",
        "flow_name": report.flow_name,
        "severity": report.severity,
        "created_at": _iso(report.created_at),
        "payload": json.dumps(
            {
                "id": report.id,
                "flow_name": report.flow_name,
                "severity": report.severity,
                "error_type": report.error_type,
                "error_message": report.error_message,
                "traceback": report.traceback,
                "logs": report.logs,
                "context": report.context,
                "error_signature": report.error_signature,
                "dedup_count": report.dedup_count,
                "cascade_source": report.cascade_source,
                "created_at": _iso(report.created_at),
            }
        ),
    }
    # DynamoDB GSI keys cannot be empty strings — omit when unset.
    if report.error_signature:
        item["error_signature"] = report.error_signature
    return item


def _item_to_report(item: Dict[str, Any]) -> Report:
    data = json.loads(item["payload"])
    return Report(
        id=data["id"],
        flow_name=data["flow_name"],
        severity=data["severity"],
        error_type=data.get("error_type"),
        error_message=data["error_message"],
        traceback=data.get("traceback"),
        logs=data.get("logs"),
        context=data.get("context"),
        error_signature=data.get("error_signature"),
        dedup_count=data.get("dedup_count", 1),
        cascade_source=data.get("cascade_source"),
        created_at=_parse_dt(data["created_at"]),
    )


class DynamoDBStorage(StorageBackend):
    """Shared DynamoDB-backed store for distributed flow-doctor runtimes."""

    def __init__(
        self,
        table_name: str,
        *,
        region: Optional[str] = None,
    ):
        if boto3 is None:
            raise ImportError(
                "DynamoDB store requires boto3. Install flow-doctor[remediation] or boto3."
            )
        self.table_name = table_name
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._ddb = boto3.resource("dynamodb", region_name=self.region)
        self._table = self._ddb.Table(table_name)

    def init_schema(self) -> None:
        """Create the table when missing (local/moto). Production uses IaC."""
        try:
            self._table.load()
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ResourceNotFoundException", "404"):
                raise
        client = self._ddb.meta.client
        client.create_table(
            TableName=self.table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "error_signature", "AttributeType": "S"},
                {"AttributeName": "flow_name", "AttributeType": "S"},
                {"AttributeName": "action_type", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SignatureIndex",
                    "KeySchema": [
                        {"AttributeName": "error_signature", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "FlowIndex",
                    "KeySchema": [
                        {"AttributeName": "flow_name", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "ActionTypeIndex",
                    "KeySchema": [
                        {"AttributeName": "action_type", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        client.get_waiter("table_exists").wait(TableName=self.table_name)
        self._table = self._ddb.Table(self.table_name)

    def save_report(self, report: Report) -> None:
        self._table.put_item(Item=_report_to_item(report))

    def find_report_by_signature(
        self,
        error_signature: str,
        since: datetime,
    ) -> Optional[Report]:
        if not error_signature:
            return None
        resp = self._table.query(
            IndexName="SignatureIndex",
            KeyConditionExpression=(
                "error_signature = :sig AND created_at >= :since"
            ),
            ExpressionAttributeValues={
                ":sig": error_signature,
                ":since": _iso(since),
            },
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items") or []
        if not items:
            return None
        return _item_to_report(items[0])

    def increment_dedup_count(self, report_id: str) -> None:
        item = self._table.get_item(Key={"pk": f"REPORT#{report_id}", "sk": "META"}).get("Item")
        if not item:
            return
        report = _item_to_report(item)
        report.dedup_count += 1
        self.save_report(report)

    def save_action(self, action: Action) -> None:
        self._table.put_item(
            Item={
                "pk": f"ACTION#{action.id}",
                "sk": "META",
                "entity": "action",
                "action_type": action.action_type,
                "created_at": _iso(action.created_at),
                "payload": json.dumps(
                    {
                        "id": action.id,
                        "report_id": action.report_id,
                        "diagnosis_id": action.diagnosis_id,
                        "action_type": action.action_type,
                        "target": action.target,
                        "status": action.status,
                        "metadata": action.metadata,
                        "created_at": _iso(action.created_at),
                    }
                ),
            }
        )

    def save_decision(self, decision: Decision) -> None:
        self._table.put_item(
            Item={
                "pk": f"DECISION#{decision.id}",
                "sk": "META",
                "entity": "decision",
                "flow_name": decision.flow_name,
                "created_at": _iso(decision.created_at),
                "payload": json.dumps(
                    {
                        "id": decision.id,
                        "report_id": decision.report_id,
                        "flow_name": decision.flow_name,
                        "error_signature": decision.error_signature,
                        "reason": decision.reason,
                        "detail": decision.detail,
                        "created_at": _iso(decision.created_at),
                    }
                ),
            }
        )

    def decision_breakdown_today(self, flow_name: Optional[str] = None) -> Dict[str, int]:
        today = date.today().isoformat()
        if flow_name:
            resp = self._table.query(
                IndexName="FlowIndex",
                KeyConditionExpression="flow_name = :flow AND created_at >= :today",
                FilterExpression="entity = :entity",
                ExpressionAttributeValues={
                    ":flow": flow_name,
                    ":today": today,
                    ":entity": "decision",
                },
            )
        else:
            resp = self._table.scan(
                FilterExpression="entity = :entity AND created_at >= :today",
                ExpressionAttributeValues={
                    ":entity": "decision",
                    ":today": today,
                },
            )
        counts: Dict[str, int] = {}
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            reason = data["reason"]
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def count_actions_today(self, action_type: str) -> int:
        today_start = datetime.combine(date.today(), datetime.min.time()).isoformat()
        resp = self._table.query(
            IndexName="ActionTypeIndex",
            KeyConditionExpression="action_type = :atype AND created_at >= :today",
            ExpressionAttributeValues={
                ":atype": action_type,
                ":today": today_start,
            },
            Select="COUNT",
        )
        return int(resp.get("Count", 0))

    def has_recent_failure(self, flow_name: str, since: datetime) -> bool:
        resp = self._table.query(
            IndexName="FlowIndex",
            KeyConditionExpression="flow_name = :flow AND created_at >= :since",
            FilterExpression="entity = :entity AND severity IN (:err, :crit)",
            ExpressionAttributeValues={
                ":flow": flow_name,
                ":since": _iso(since),
                ":entity": "report",
                ":err": "error",
                ":crit": "critical",
            },
            Limit=1,
        )
        return bool(resp.get("Items"))

    def get_reports(
        self,
        flow_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Report]:
        if flow_name:
            resp = self._table.query(
                IndexName="FlowIndex",
                KeyConditionExpression="flow_name = :flow",
                FilterExpression="entity = :entity",
                ExpressionAttributeValues={
                    ":flow": flow_name,
                    ":entity": "report",
                },
                ScanIndexForward=False,
                Limit=limit,
            )
        else:
            resp = self._table.scan(
                FilterExpression="entity = :entity",
                ExpressionAttributeValues={":entity": "report"},
                Limit=limit,
            )
        return [_item_to_report(item) for item in resp.get("Items") or []]

    def get_report(self, report_id: str) -> Optional[Report]:
        item = self._table.get_item(Key={"pk": f"REPORT#{report_id}", "sk": "META"}).get("Item")
        if not item:
            return None
        return _item_to_report(item)

    def count_reports_today(self, flow_name: Optional[str] = None) -> int:
        today = date.today().isoformat()
        if flow_name:
            resp = self._table.query(
                IndexName="FlowIndex",
                KeyConditionExpression="flow_name = :flow AND created_at >= :today",
                FilterExpression="entity = :entity",
                ExpressionAttributeValues={
                    ":flow": flow_name,
                    ":today": today,
                    ":entity": "report",
                },
                Select="COUNT",
            )
        else:
            resp = self._table.scan(
                FilterExpression="entity = :entity AND created_at >= :today",
                ExpressionAttributeValues={
                    ":entity": "report",
                    ":today": today,
                },
                Select="COUNT",
            )
        return int(resp.get("Count", 0))

    def count_diagnoses_today(self) -> int:
        today = date.today().isoformat()
        resp = self._table.scan(
            FilterExpression="entity = :entity AND created_at >= :today",
            ExpressionAttributeValues={
                ":entity": "diagnosis",
                ":today": today,
            },
            Select="COUNT",
        )
        return int(resp.get("Count", 0))

    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        self._table.put_item(
            Item={
                "pk": f"DIAGNOSIS#{diagnosis.id}",
                "sk": "META",
                "entity": "diagnosis",
                "created_at": _iso(diagnosis.created_at),
                "payload": json.dumps(
                    {
                        "id": diagnosis.id,
                        "report_id": diagnosis.report_id,
                        "flow_name": diagnosis.flow_name,
                        "category": diagnosis.category,
                        "root_cause": diagnosis.root_cause,
                        "affected_files": diagnosis.affected_files,
                        "confidence": diagnosis.confidence,
                        "remediation": diagnosis.remediation,
                        "auto_fixable": diagnosis.auto_fixable,
                        "reasoning": diagnosis.reasoning,
                        "alternative_hypotheses": diagnosis.alternative_hypotheses,
                        "source": diagnosis.source,
                        "llm_model": diagnosis.llm_model,
                        "tokens_used": diagnosis.tokens_used,
                        "cost_usd": diagnosis.cost_usd,
                        "created_at": _iso(diagnosis.created_at),
                    }
                ),
            }
        )

    def get_diagnosis_by_report(self, report_id: str) -> Optional[Diagnosis]:
        resp = self._table.scan(
            FilterExpression="entity = :entity",
            ExpressionAttributeValues={":entity": "diagnosis"},
        )
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            if data.get("report_id") == report_id:
                return Diagnosis(
                    id=data["id"],
                    report_id=data["report_id"],
                    flow_name=data["flow_name"],
                    category=data["category"],
                    root_cause=data["root_cause"],
                    affected_files=data.get("affected_files"),
                    confidence=data["confidence"],
                    remediation=data.get("remediation"),
                    auto_fixable=data.get("auto_fixable"),
                    reasoning=data.get("reasoning"),
                    alternative_hypotheses=data.get("alternative_hypotheses"),
                    source=data.get("source", "llm"),
                    llm_model=data.get("llm_model"),
                    tokens_used=data.get("tokens_used"),
                    cost_usd=data.get("cost_usd"),
                    created_at=_parse_dt(data["created_at"]),
                )
        return None

    def find_known_pattern(self, error_signature: str) -> Optional[KnownPattern]:
        resp = self._table.scan(
            FilterExpression="entity = :entity",
            ExpressionAttributeValues={":entity": "known_pattern"},
        )
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            if data.get("error_signature") == error_signature:
                return KnownPattern(
                    id=data["id"],
                    flow_name=data.get("flow_name"),
                    error_signature=data["error_signature"],
                    category=data["category"],
                    root_cause=data["root_cause"],
                    resolution=data.get("resolution"),
                    auto_fixable=data.get("auto_fixable", False),
                    hit_count=data.get("hit_count", 0),
                    last_seen=_parse_dt(data["last_seen"]) if data.get("last_seen") else None,
                    created_at=_parse_dt(data["created_at"]),
                )
        return None

    def save_known_pattern(self, pattern: KnownPattern) -> None:
        self._table.put_item(
            Item={
                "pk": f"PATTERN#{pattern.id}",
                "sk": "META",
                "entity": "known_pattern",
                "created_at": _iso(pattern.created_at),
                "payload": json.dumps(
                    {
                        "id": pattern.id,
                        "flow_name": pattern.flow_name,
                        "error_signature": pattern.error_signature,
                        "category": pattern.category,
                        "root_cause": pattern.root_cause,
                        "resolution": pattern.resolution,
                        "auto_fixable": pattern.auto_fixable,
                        "hit_count": pattern.hit_count,
                        "last_seen": _iso(pattern.last_seen) if pattern.last_seen else None,
                        "created_at": _iso(pattern.created_at),
                    }
                ),
            }
        )

    def increment_pattern_hit(self, pattern_id: str) -> None:
        item = self._table.get_item(Key={"pk": f"PATTERN#{pattern_id}", "sk": "META"}).get("Item")
        if not item:
            return
        data = json.loads(item["payload"])
        data["hit_count"] = data.get("hit_count", 0) + 1
        data["last_seen"] = _iso(datetime.utcnow())
        item["payload"] = json.dumps(data)
        self._table.put_item(Item=item)

    def get_degraded_actions(self, since: datetime) -> List[Action]:
        resp = self._table.scan(
            FilterExpression="entity = :entity AND created_at >= :since",
            ExpressionAttributeValues={
                ":entity": "action",
                ":since": _iso(since),
            },
        )
        actions: List[Action] = []
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            if data.get("status") != "degraded":
                continue
            actions.append(
                Action(
                    id=data["id"],
                    report_id=data.get("report_id"),
                    diagnosis_id=data.get("diagnosis_id"),
                    action_type=data["action_type"],
                    target=data.get("target"),
                    status=data["status"],
                    metadata=data.get("metadata"),
                    created_at=_parse_dt(data["created_at"]),
                )
            )
        return actions

    def save_fix_attempt(self, attempt: FixAttempt) -> None:
        self._table.put_item(
            Item={
                "pk": f"FIX#{attempt.id}",
                "sk": "META",
                "entity": "fix_attempt",
                "created_at": _iso(attempt.created_at),
                "payload": json.dumps(
                    {
                        "id": attempt.id,
                        "diagnosis_id": attempt.diagnosis_id,
                        "diff": attempt.diff,
                        "test_passed": attempt.test_passed,
                        "test_output": attempt.test_output,
                        "pr_url": attempt.pr_url,
                        "pr_status": attempt.pr_status,
                        "rejection_reason": attempt.rejection_reason,
                        "created_at": _iso(attempt.created_at),
                    }
                ),
            }
        )

    def get_fix_attempts_for_diagnosis(self, diagnosis_id: str) -> List[FixAttempt]:
        resp = self._table.scan(
            FilterExpression="entity = :entity",
            ExpressionAttributeValues={":entity": "fix_attempt"},
        )
        out: List[FixAttempt] = []
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            if data.get("diagnosis_id") != diagnosis_id:
                continue
            out.append(
                FixAttempt(
                    id=data["id"],
                    diagnosis_id=data["diagnosis_id"],
                    diff=data["diff"],
                    test_passed=data.get("test_passed"),
                    test_output=data.get("test_output"),
                    pr_url=data.get("pr_url"),
                    pr_status=data.get("pr_status"),
                    rejection_reason=data.get("rejection_reason"),
                    created_at=_parse_dt(data["created_at"]),
                )
            )
        return out

    def save_remediation_action(
        self,
        report_id: str,
        diagnosis_id: str,
        decision_type: str,
        playbook_pattern: str = None,
        action_type: str = None,
        commands: list = None,
        dry_run: bool = True,
        success: bool = None,
        output: str = None,
        error: str = None,
    ) -> str:
        from flow_doctor.core.models import _ulid

        action_id = _ulid()
        created = datetime.utcnow()
        self._table.put_item(
            Item={
                "pk": f"REMEDIATION#{action_id}",
                "sk": "META",
                "entity": "remediation_action",
                "created_at": _iso(created),
                "payload": json.dumps(
                    {
                        "id": action_id,
                        "report_id": report_id,
                        "diagnosis_id": diagnosis_id,
                        "decision_type": decision_type,
                        "playbook_pattern": playbook_pattern,
                        "action_type": action_type,
                        "commands": commands,
                        "dry_run": dry_run,
                        "success": success,
                        "output": output,
                        "error": error,
                        "created_at": _iso(created),
                    }
                ),
            }
        )
        return action_id

    def get_daily_diagnosis_cost(self) -> float:
        today = date.today().isoformat()
        resp = self._table.scan(
            FilterExpression="entity = :entity AND created_at >= :today",
            ExpressionAttributeValues={
                ":entity": "diagnosis",
                ":today": today,
            },
        )
        total = 0.0
        for item in resp.get("Items") or []:
            data = json.loads(item["payload"])
            total += float(data.get("cost_usd") or 0.0)
        return total

    def count_remediations_today(self) -> int:
        today = date.today().isoformat()
        resp = self._table.scan(
            FilterExpression=(
                "entity = :entity AND created_at >= :today AND decision_type = :dtype"
            ),
            ExpressionAttributeValues={
                ":entity": "remediation_action",
                ":today": today,
                ":dtype": "auto_remediate",
            },
            Select="COUNT",
        )
        return int(resp.get("Count", 0))
