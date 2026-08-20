import importlib.util
import os
import sqlite3
import sys
import types


def load_runtime(tmp_path):
    app = types.SimpleNamespace()
    app.DB_PATH = str(tmp_path / "academy.sqlite3")
    app.DB_LOCK = __import__("threading").RLock()
    app.get_db_connection = lambda: sqlite3.connect(
        app.DB_PATH, timeout=10.0, check_same_thread=False
    )

    conn = sqlite3.connect(app.DB_PATH)
    conn.execute(
        """
        CREATE TABLE academy_courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            lessons INTEGER,
            duration_text TEXT,
            days_per_week INTEGER,
            price INTEGER,
            first_payment INTEGER,
            start_date TEXT,
            topics TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE course_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            schedule_text TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE academy_info (
            info_key TEXT PRIMARY KEY,
            info_value TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE admin_pending_actions (
            sender_id TEXT PRIMARY KEY,
            action_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    module_path = os.path.join(os.path.dirname(__file__), "admin_runtime.py")
    spec = importlib.util.spec_from_file_location("admin_runtime", module_path)
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    sys.modules["admin_runtime"] = base

    hotfix_path = os.path.join(os.path.dirname(__file__), "admin_runtime_hotfix.py")
    hotfix_spec = importlib.util.spec_from_file_location("admin_runtime_hotfix", hotfix_path)
    hotfix = importlib.util.module_from_spec(hotfix_spec)
    hotfix_spec.loader.exec_module(hotfix)
    hotfix.apply_patch(app)
    return base, hotfix, app


def test_parse_update_course_is_structured(tmp_path):
    runtime, _, _ = load_runtime(tmp_path)
    parsed = runtime.parse_admin_command(
        "غيّر سعر دورة ميك أب احترافي إلى 850000 وغيّر الدفعة الأولى إلى 250000"
    )
    assert parsed["tool"] == "update_course"
    assert parsed["args"]["name"] == "ميك أب احترافي"
    assert parsed["args"]["price"] == 850000
    assert parsed["args"]["first_payment"] == 250000


def test_create_and_update_course_records_history(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    result = runtime.execute_structured(app, "admin1", "أضف دورة اختبار، 12 درس، السعر 500000، الدفعة الأولى 100000، تبدأ 2026-09-01")
    assert result["ok"] is True
    result = runtime.execute_structured(app, "admin1", "غيّر سعر دورة اختبار إلى 650000")
    assert result["ok"] is True
    conn = sqlite3.connect(app.DB_PATH)
    course = conn.execute("SELECT price FROM academy_courses WHERE name='اختبار'").fetchone()
    history_count = conn.execute("SELECT COUNT(*) FROM academy_change_history WHERE entity_type='course'").fetchone()[0]
    conn.close()
    assert course[0] == 650000
    assert history_count >= 2


def test_rollback_restores_previous_course_value(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    runtime.execute_structured(app, "admin1", "أضف دورة اختبار، 12 درس، السعر 500000")
    runtime.execute_structured(app, "admin1", "غيّر سعر دورة اختبار إلى 650000")
    result = runtime.execute_structured(app, "admin1", "تراجع عن آخر تعديل لدورة اختبار")
    assert result["ok"] is True
    conn = sqlite3.connect(app.DB_PATH)
    price = conn.execute("SELECT price FROM academy_courses WHERE name='اختبار'").fetchone()[0]
    conn.close()
    assert price == 500000


def test_offer_is_date_bounded(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    result = runtime.execute_structured(
        app,
        "admin1",
        "أضف عرض مكياج: خصم 100000، من 2026-09-01 إلى 2026-09-10",
    )
    assert result["ok"] is True
    conn = sqlite3.connect(app.DB_PATH)
    row = conn.execute("SELECT title, starts_at, ends_at FROM academy_offers").fetchone()
    conn.close()
    assert row == ("مكياج", "2026-09-01", "2026-09-10")


def test_lead_scoring_moves_hot_for_high_intent(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    runtime.upsert_lead(app, "customer1", "بدي دورة ميك أب. شو السعر؟ وكيف ثبت عن بعد عبر شام كاش؟")
    lead = runtime.get_lead(app, "customer1")
    assert lead["score"] >= 70
    assert lead["stage"] == "hot"


def test_admin_lead_report_is_sorted(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    runtime.upsert_lead(app, "low", "مرحبا")
    runtime.upsert_lead(app, "high", "بدي السعر والتثبيت والشام كاش لدورة الميك أب")
    leads = runtime.list_leads(app, limit=10)
    assert leads[0]["sender_id"] == "high"


def test_unknown_admin_text_falls_back_without_mutation(tmp_path):
    runtime, _, app = load_runtime(tmp_path)
    result = runtime.route_admin_command(app, "admin1", "هاي رسالة ما إلها أمر واضح")
    assert result["handled"] is False
