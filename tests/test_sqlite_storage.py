

def test_init_schema_migrates_a_legacy_actions_table():
    """`CREATE TABLE IF NOT EXISTS` is a no-op on an existing database, so a
    store created before `actions.flow_name` existed would keep the old shape
    and every insert would fail on the new column list
    (alpha-engine-config-I6921).

    The ALTER is the one branch a fresh test database never reaches, which is
    exactly why it needs an explicit test: it only ever runs in the field.
    """
    import sqlite3
    import tempfile

    from flow_doctor.core.models import Action
    from flow_doctor.storage.sqlite import SQLiteStorage

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(f.name)
    conn.execute(
        """CREATE TABLE actions (
               id TEXT PRIMARY KEY, report_id TEXT, diagnosis_id TEXT,
               action_type TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
               metadata TEXT, created_at TEXT NOT NULL)"""
    )
    conn.commit()
    conn.close()

    store = SQLiteStorage(f.name)
    store.init_schema()

    cols = {
        r["name"]
        for r in store._conn().execute("PRAGMA table_info(actions)")
    }
    assert "flow_name" in cols

    store.save_action(
        Action(
            report_id="r", action_type="email_alert",
            status="sent", flow_name="executor",
        )
    )
    assert store.count_actions_today("email_alert", "executor") == 1


def test_init_schema_is_idempotent_over_the_migration():
    """Run twice: the ALTER must not fire a second time and raise
    'duplicate column name'."""
    import tempfile

    from flow_doctor.storage.sqlite import SQLiteStorage

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    store.init_schema()

    cols = {
        r["name"]
        for r in store._conn().execute("PRAGMA table_info(actions)")
    }
    assert "flow_name" in cols


def test_count_actions_today_can_exclude_failed_deliveries():
    """alpha-engine-config-I12711: both backends answer the rate limiter's
    question the same way."""
    import tempfile

    from flow_doctor.core.models import Action
    from flow_doctor.storage.sqlite import SQLiteStorage

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    for status in ("failed", "failed", "sent", "degraded"):
        store.save_action(
            Action(
                report_id="r", action_type="telegram_alert",
                status=status, flow_name="executor",
            )
        )
    assert store.count_actions_today("telegram_alert", "executor") == 4
    assert store.count_actions_today(
        "telegram_alert", "executor", exclude_status="failed"
    ) == 2
    assert store.count_actions_today("telegram_alert", exclude_status="failed") == 2
