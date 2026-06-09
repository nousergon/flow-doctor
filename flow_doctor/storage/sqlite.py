"""SQLite storage backend."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, date
from typing import List, Optional

from typing import Dict

from flow_doctor.core.models import (
    Action,
    Decision,
    Diagnosis,
    FixAttempt,
    KnownPattern,
    Report,
)
from flow_doctor.storage.base import StorageBackend

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    flow_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    error_type      TEXT,
    error_message   TEXT NOT NULL,
    traceback       TEXT,
    logs            TEXT,
    context         TEXT,
    error_signature TEXT,
    dedup_count     INTEGER DEFAULT 1,
    cascade_source  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    flow_name       TEXT NOT NULL,
    category        TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    affected_files  TEXT,
    confidence      REAL NOT NULL,
    remediation     TEXT,
    auto_fixable    INTEGER,
    reasoning       TEXT,
    alternative_hypotheses TEXT,
    source          TEXT NOT NULL,
    llm_model       TEXT,
    tokens_used     INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    action_type     TEXT NOT NULL,
    target          TEXT,
    status          TEXT NOT NULL,
    metadata        TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT,
    flow_name       TEXT NOT NULL,
    error_signature TEXT,
    reason          TEXT NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    correct         INTEGER NOT NULL,
    corrected_category    TEXT,
    corrected_root_cause  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_patterns (
    id              TEXT PRIMARY KEY,
    flow_name       TEXT,
    error_signature TEXT NOT NULL,
    category        TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    resolution      TEXT,
    auto_fixable    INTEGER DEFAULT 0,
    hit_count       INTEGER DEFAULT 0,
    last_seen       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fix_attempts (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    diff            TEXT NOT NULL,
    test_passed     INTEGER,
    test_output     TEXT,
    pr_url          TEXT,
    pr_status       TEXT,
    rejection_reason TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remediation_actions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    decision_type   TEXT NOT NULL,
    playbook_pattern TEXT,
    action_type     TEXT,
    commands        TEXT,
    dry_run         INTEGER DEFAULT 1,
    success         INTEGER,
    output          TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_flow_created ON reports(flow_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_signature ON reports(error_signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diagnoses_report ON diagnoses(report_id);
CREATE INDEX IF NOT EXISTS idx_known_patterns_sig ON known_patterns(error_signature);
CREATE INDEX IF NOT EXISTS idx_fix_attempts_diagnosis ON fix_attempts(diagnosis_id);
CREATE INDEX IF NOT EXISTS idx_actions_type_created ON actions(action_type, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_flow_created ON decisions(flow_name, created_at);
"""


class SQLiteStorage(StorageBackend):
    """SQLite-backed storage. Thread-safe via per-thread connections."""

    def __init__(self, db_path: str = "flow_doctor.db"):
        self.db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def save_report(self, report: Report) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO reports
               (id, flow_name, severity, error_type, error_message, traceback,
                logs, context, error_signature, dedup_count, cascade_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report.id,
                report.flow_name,
                report.severity,
                report.error_type,
                report.error_message,
                report.traceback,
                report.logs,
                json.dumps(report.context) if report.context else None,
                report.error_signature,
                report.dedup_count,
                report.cascade_source,
                report.created_at.isoformat(),
            ),
        )
        conn.commit()

    def save_action(self, action: Action) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO actions
               (id, report_id, diagnosis_id, action_type, target, status, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action.id,
                action.report_id,
                action.diagnosis_id,
                action.action_type,
                action.target,
                action.status,
                json.dumps(action.metadata) if action.metadata else None,
                action.created_at.isoformat(),
            ),
        )
        conn.commit()

    def save_decision(self, decision: Decision) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO decisions
               (id, report_id, flow_name, error_signature, reason, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.id,
                decision.report_id,
                decision.flow_name,
                decision.error_signature,
                decision.reason,
                decision.detail,
                decision.created_at.isoformat(),
            ),
        )
        conn.commit()

    def decision_breakdown_today(self, flow_name: Optional[str] = None) -> Dict[str, int]:
        conn = self._conn()
        today = date.today().isoformat()
        if flow_name:
            rows = conn.execute(
                """SELECT reason, COUNT(*) AS cnt FROM decisions
                   WHERE flow_name = ? AND created_at >= ? GROUP BY reason""",
                (flow_name, today),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT reason, COUNT(*) AS cnt FROM decisions
                   WHERE created_at >= ? GROUP BY reason""",
                (today,),
            ).fetchall()
        return {r["reason"]: r["cnt"] for r in rows}

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
        conn = self._conn()
        conn.execute(
            """INSERT INTO remediation_actions
               (id, report_id, diagnosis_id, decision_type, playbook_pattern,
                action_type, commands, dry_run, success, output, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action_id,
                report_id,
                diagnosis_id,
                decision_type,
                playbook_pattern,
                action_type,
                json.dumps(commands) if commands else None,
                1 if dry_run else 0,
                1 if success else (0 if success is False else None),
                output,
                error,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        return action_id

    def get_daily_diagnosis_cost(self) -> float:
        """Sum of cost_usd for all diagnoses created today."""
        conn = self._conn()
        today = date.today().isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM diagnoses WHERE created_at >= ?",
            (today,),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def count_remediations_today(self) -> int:
        """Count auto-remediation actions executed today (persistent daily limit)."""
        conn = self._conn()
        today = date.today().isoformat()
        row = conn.execute(
            """SELECT COUNT(*) FROM remediation_actions
               WHERE decision_type = 'auto_remediate' AND created_at >= ?""",
            (today,),
        ).fetchone()
        return row[0] if row else 0

    def find_report_by_signature(
        self,
        error_signature: str,
        since: datetime,
    ) -> Optional[Report]:
        conn = self._conn()
        row = conn.execute(
            """SELECT * FROM reports
               WHERE error_signature = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (error_signature, since.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_report(row)

    def increment_dedup_count(self, report_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE reports SET dedup_count = dedup_count + 1 WHERE id = ?",
            (report_id,),
        )
        conn.commit()

    def count_reports_today(self, flow_name: Optional[str] = None) -> int:
        """Count reports created today, optionally filtered by flow_name."""
        conn = self._conn()
        today = date.today().isoformat()
        if flow_name:
            row = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE flow_name = ? AND created_at >= ?",
                (flow_name, today),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE created_at >= ?",
                (today,),
            ).fetchone()
        return row[0] if row else 0

    def count_diagnoses_today(self) -> int:
        """Count diagnoses created today."""
        conn = self._conn()
        today = date.today().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM diagnoses WHERE created_at >= ?",
            (today,),
        ).fetchone()
        return row[0] if row else 0

    def count_actions_today(self, action_type: str) -> int:
        conn = self._conn()
        today_start = datetime.combine(date.today(), datetime.min.time()).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM actions WHERE action_type = ? AND created_at >= ?",
            (action_type, today_start),
        ).fetchone()
        return row["cnt"] if row else 0

    def has_recent_failure(self, flow_name: str, since: datetime) -> bool:
        conn = self._conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM reports
               WHERE flow_name = ? AND severity IN ('error', 'critical')
               AND created_at >= ?""",
            (flow_name, since.isoformat()),
        ).fetchone()
        return (row["cnt"] if row else 0) > 0

    def get_reports(
        self,
        flow_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Report]:
        conn = self._conn()
        if flow_name:
            rows = conn.execute(
                "SELECT * FROM reports WHERE flow_name = ? ORDER BY created_at DESC LIMIT ?",
                (flow_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_report(r) for r in rows]

    def get_report(self, report_id: str) -> Optional[Report]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_report(row)

    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO diagnoses
               (id, report_id, flow_name, category, root_cause, affected_files,
                confidence, remediation, auto_fixable, reasoning,
                alternative_hypotheses, source, llm_model, tokens_used, cost_usd,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                diagnosis.id,
                diagnosis.report_id,
                diagnosis.flow_name,
                diagnosis.category,
                diagnosis.root_cause,
                json.dumps(diagnosis.affected_files) if diagnosis.affected_files else None,
                diagnosis.confidence,
                diagnosis.remediation,
                1 if diagnosis.auto_fixable else 0 if diagnosis.auto_fixable is not None else None,
                diagnosis.reasoning,
                json.dumps(diagnosis.alternative_hypotheses) if diagnosis.alternative_hypotheses else None,
                diagnosis.source,
                diagnosis.llm_model,
                diagnosis.tokens_used,
                diagnosis.cost_usd,
                diagnosis.created_at.isoformat(),
            ),
        )
        conn.commit()

    def get_diagnosis_by_report(self, report_id: str) -> Optional[Diagnosis]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM diagnoses WHERE report_id = ? ORDER BY created_at DESC LIMIT 1",
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_diagnosis(row)

    def find_known_pattern(self, error_signature: str) -> Optional[KnownPattern]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM known_patterns WHERE error_signature = ? LIMIT 1",
            (error_signature,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_known_pattern(row)

    def save_known_pattern(self, pattern: KnownPattern) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO known_patterns
               (id, flow_name, error_signature, category, root_cause, resolution,
                auto_fixable, hit_count, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pattern.id,
                pattern.flow_name,
                pattern.error_signature,
                pattern.category,
                pattern.root_cause,
                pattern.resolution,
                1 if pattern.auto_fixable else 0,
                pattern.hit_count,
                pattern.last_seen.isoformat() if pattern.last_seen else None,
                pattern.created_at.isoformat(),
            ),
        )
        conn.commit()

    def increment_pattern_hit(self, pattern_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE known_patterns SET hit_count = hit_count + 1, last_seen = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), pattern_id),
        )
        conn.commit()

    def get_degraded_actions(self, since: datetime) -> List[Action]:
        conn = self._conn()
        rows = conn.execute(
            """SELECT * FROM actions
               WHERE status = 'degraded' AND created_at >= ?
               ORDER BY created_at DESC""",
            (since.isoformat(),),
        ).fetchall()
        return [self._row_to_action(r) for r in rows]

    def save_fix_attempt(self, attempt: FixAttempt) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO fix_attempts
               (id, diagnosis_id, diff, test_passed, test_output, pr_url,
                pr_status, rejection_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt.id,
                attempt.diagnosis_id,
                attempt.diff,
                1 if attempt.test_passed else 0 if attempt.test_passed is not None else None,
                attempt.test_output,
                attempt.pr_url,
                attempt.pr_status,
                attempt.rejection_reason,
                attempt.created_at.isoformat(),
            ),
        )
        conn.commit()

    def get_fix_attempts_for_diagnosis(self, diagnosis_id: str) -> List[FixAttempt]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM fix_attempts WHERE diagnosis_id = ? ORDER BY created_at DESC",
            (diagnosis_id,),
        ).fetchall()
        return [self._row_to_fix_attempt(r) for r in rows]

    @staticmethod
    def _row_to_diagnosis(row: sqlite3.Row) -> Diagnosis:
        affected = row["affected_files"]
        alt = row["alternative_hypotheses"]
        auto_fix = row["auto_fixable"]
        return Diagnosis(
            id=row["id"],
            report_id=row["report_id"],
            flow_name=row["flow_name"],
            category=row["category"],
            root_cause=row["root_cause"],
            affected_files=json.loads(affected) if affected else None,
            confidence=row["confidence"],
            remediation=row["remediation"],
            auto_fixable=bool(auto_fix) if auto_fix is not None else None,
            reasoning=row["reasoning"],
            alternative_hypotheses=json.loads(alt) if alt else None,
            source=row["source"],
            llm_model=row["llm_model"],
            tokens_used=row["tokens_used"],
            cost_usd=row["cost_usd"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_known_pattern(row: sqlite3.Row) -> KnownPattern:
        last_seen = row["last_seen"]
        return KnownPattern(
            id=row["id"],
            flow_name=row["flow_name"],
            error_signature=row["error_signature"],
            category=row["category"],
            root_cause=row["root_cause"],
            resolution=row["resolution"],
            auto_fixable=bool(row["auto_fixable"]),
            hit_count=row["hit_count"],
            last_seen=datetime.fromisoformat(last_seen) if last_seen else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> Action:
        metadata = row["metadata"]
        return Action(
            id=row["id"],
            report_id=row["report_id"],
            action_type=row["action_type"],
            status=row["status"],
            diagnosis_id=row["diagnosis_id"],
            target=row["target"],
            metadata=json.loads(metadata) if metadata else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_fix_attempt(row: sqlite3.Row) -> FixAttempt:
        test_passed = row["test_passed"]
        return FixAttempt(
            id=row["id"],
            diagnosis_id=row["diagnosis_id"],
            diff=row["diff"],
            test_passed=bool(test_passed) if test_passed is not None else None,
            test_output=row["test_output"],
            pr_url=row["pr_url"],
            pr_status=row["pr_status"],
            rejection_reason=row["rejection_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        ctx = row["context"]
        return Report(
            id=row["id"],
            flow_name=row["flow_name"],
            severity=row["severity"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            traceback=row["traceback"],
            logs=row["logs"],
            context=json.loads(ctx) if ctx else None,
            error_signature=row["error_signature"],
            dedup_count=row["dedup_count"],
            cascade_source=row["cascade_source"] if "cascade_source" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
