"""Runtime extension for the existing Messenger academy bot.

The module deliberately does not replace app.py. Gunicorn loads this module after
app.py is initialized and it wraps the existing admin and AI entry points.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import date
from typing import Any, Dict, Optional


EXTENSION_LOCK = threading.RLock()
PATCH_MARKER = "_admin_runtime_v2_patched"
CONFIRM_TTL_SECONDS = 120


def _conn(app):
    return app.get_db_connection()


def init_extension_schema(app):
    """Create only additive tables; preserve all existing app tables."""
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS academy_change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_sender_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS academy_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_leads (
                    sender_id TEXT PRIMARY KEY,
                    score INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'cold',
                    interested_course TEXT,
                    last_message TEXT,
                    messages_count INTEGER NOT NULL DEFAULT 0,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_score ON customer_leads(score DESC, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_change_history_entity ON academy_change_history(entity_type, entity_key, id DESC)"
            )
            conn.commit()
        finally:
            conn.close()


def _normalize(text: str) -> str:
    text = (text or "").strip()
    for a, b in {"أ": "ا", "إ": "ا", "آ": "ا", "،": ",", "؛": ";"}.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


def _money(value: str | int | None) -> Optional[int]:
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _date(value: str | None) -> Optional[str]:
    if not value:
        return None
    value = value.strip().replace("/", "-")
    if not re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", value):
        return None
    y, m, d = [int(x) for x in value.split("-")]
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _course_name(text: str) -> Optional[str]:
    patterns = [
        r"(?:دورة)\s+(.+?)(?=\s+(?:الي|الى|إلى|ل|بسعر|سعر|الدفعة|دفعة|تبدا|تبدأ|بداية|بسعر)|[,;]|$)",
        r"(?:دورة)\s+(.+?)(?=\s*[:,])",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .،,")
            if value:
                return value
    return None


def _field(text: str, *labels: str) -> Optional[str]:
    label = "|".join(re.escape(x) for x in labels)
    m = re.search(rf"(?:{label})\s*[:=]?\s*([^,;\n]+)", text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def _int_field(text: str, *labels: str) -> Optional[int]:
    raw = _field(text, *labels)
    if raw:
        return _money(raw)
    label = "|".join(re.escape(x) for x in labels)
    m = re.search(rf"([0-9][0-9,]*)\s*(?:{label})", text, flags=re.IGNORECASE)
    return _money(m.group(1)) if m else None


def _extract_date_from(text: str, labels: tuple[str, ...] = ("تبدا", "تبدأ", "البداية", "بدء", "من")) -> Optional[str]:
    label = "|".join(re.escape(x) for x in labels)
    m = re.search(rf"(?:{label})?\s*[:=]?\s*(20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}})", text, flags=re.IGNORECASE)
    return _date(m.group(1)) if m else None


def _find_course(conn, name: str):
    return conn.execute(
        "SELECT * FROM academy_courses WHERE lower(name)=lower(?) LIMIT 1", (name,)
    ).fetchone()


def _course_dict(row) -> Optional[dict]:
    if not row:
        return None
    fields = [
        "id", "name", "lessons", "duration_text", "days_per_week",
        "price", "first_payment", "start_date", "topics", "active",
        "created_at", "updated_at",
    ]
    return dict(zip(fields, row))


def _snapshot_course(conn, name: str) -> Optional[dict]:
    return _course_dict(_find_course(conn, name))


def _history(app, admin_id: str, entity_type: str, entity_key: str, action: str, before: Any, after: Any):
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            conn.execute(
                """
                INSERT INTO academy_change_history
                (admin_sender_id, entity_type, entity_key, action, before_json, after_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_id,
                    entity_type,
                    entity_key,
                    action,
                    json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _pending_confirmation(app, sender_id: str, action: dict):
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO admin_pending_actions(sender_id, action_json, expires_at) VALUES (?,?,?)",
                (sender_id, json.dumps(action, ensure_ascii=False), time.time() + CONFIRM_TTL_SECONDS),
            )
            conn.commit()
        finally:
            conn.close()


def _pop_confirmation(app, sender_id: str) -> Optional[dict]:
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            row = conn.execute(
                "SELECT action_json, expires_at FROM admin_pending_actions WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
            conn.execute("DELETE FROM admin_pending_actions WHERE sender_id=?", (sender_id,))
            conn.commit()
        finally:
            conn.close()
    if not row or time.time() > float(row[1]):
        return None
    return json.loads(row[0])


def _parse_update_course(text: str) -> Optional[dict]:
    low = _normalize(text).lower()
    if not ("غير" in low or "عدل" in low or "تعديل" in low):
        return None
    if "دورة" not in low:
        return None
    name = _course_name(text)
    if not name:
        return None
    args: Dict[str, Any] = {"name": name}
    mapping = {
        "price": ("السعر", "سعر"),
        "first_payment": ("الدفعة الاولى", "دفعة اولى", "الدفعة الأولى", "دفعة أولى"),
        "lessons": ("عدد الدروس", "الدروس", "درس"),
        "days_per_week": ("ايام بالاسبوع", "ايام بالأسبوع", "أيام بالأسبوع"),
        "duration_text": ("مدة الدرس", "مدة"),
        "topics": ("المحاور", "محاور"),
    }
    for field, labels in mapping.items():
        value = _int_field(text, *labels) if field in {"price", "first_payment", "lessons", "days_per_week"} else _field(text, *labels)
        if value is not None:
            args[field] = value
    d = _extract_date_from(text)
    if d:
        args["start_date"] = d
    return {"tool": "update_course", "args": args}


def parse_admin_command(command_text: str) -> dict:
    text = _normalize(command_text)
    low = text.lower()

    if low in {"نعم", "تاكيد", "تأكيد", "موافق"}:
        return {"tool": "confirm", "args": {}}
    if low in {"لا", "الغاء", "إلغاء", "cancel"}:
        return {"tool": "cancel", "args": {}}

    if "تراجع عن اخر تعديل" in low or "تراجع عن آخر تعديل" in command_text:
        name = _course_name(text) or re.sub(r".*دورة\s+", "", text, count=1).strip()
        return {"tool": "rollback_course", "args": {"name": name}}

    update = _parse_update_course(text)
    if update:
        return update

    if low.startswith("اضف دورة") or low.startswith("أضف دورة"):
        name = _course_name(text)
        if not name:
            return {"tool": "invalid", "reason": "اسم الدورة مطلوب"}
        return {
            "tool": "add_course",
            "args": {
                "name": name,
                "lessons": _int_field(text, "عدد الدروس", "الدروس", "درس"),
                "duration_text": _field(text, "مدة الدرس", "مدة"),
                "days_per_week": _int_field(text, "ايام بالاسبوع", "أيام بالأسبوع", "ايام بالأسبوع"),
                "price": _int_field(text, "السعر", "سعر"),
                "first_payment": _int_field(text, "الدفعة الاولى", "دفعة اولى", "الدفعة الأولى"),
                "start_date": _extract_date_from(text),
                "topics": _field(text, "المحاور", "محاور"),
            },
        }

    if "موعد بدء" in low or "موعد بداية" in low:
        name = _course_name(text)
        d = _extract_date_from(text, ("موعد بدء", "موعد بداية", "تاريخ", "في"))
        if name and d:
            schedule = _field(text, "دوام", "جدول", "الجدول")
            return {"tool": "add_batch", "args": {"course": name, "start_date": d, "schedule_text": schedule}}

    if low.startswith("احذف دورة") or low.startswith("حذف دورة"):
        name = _course_name(text)
        return {"tool": "delete_course", "args": {"name": name}, "confirmation_required": True}

    if "اضف عرض" in low or "أضف عرض" in low:
        m = re.search(
            r"(?:اضف|أضف)\s+عرض\s+(.+?)(?:\s*[:=-]\s*|،\s*)(.+?)(?:\s+من\s+|\s+يبدا\s+|\s+يبدأ\s+)(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\s+(?:الى|إلى|حتى)\s+(20\d{2}[-/]\d{1,2}[-/]\d{1,2})",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            return {
                "tool": "add_offer",
                "args": {
                    "title": m.group(1).strip(),
                    "description": m.group(2).strip(),
                    "starts_at": _date(m.group(3)),
                    "ends_at": _date(m.group(4)),
                },
            }

    if low.startswith("اضف معلومة") or low.startswith("أضف معلومة") or low.startswith("عدل معلومة") or low.startswith("عدّل معلومة"):
        raw = re.sub(r"^(?:اضف|أضف|عدل|عدّل)\s+معلومة\s*", "", text, flags=re.IGNORECASE)
        if "=" in raw:
            key, value = [x.strip() for x in raw.split("=", 1)]
            return {"tool": "set_info", "args": {"key": key, "value": value}}

    if "احذف معلومة" in low:
        raw = re.sub(r".*?احذف معلومة\s*", "", text, flags=re.IGNORECASE).strip()
        return {"tool": "delete_info", "args": {"key": raw}, "confirmation_required": True}

    if "اعرض العملاء الساخنين" in low or "اعرض العملاء المهتمين" in low or "مين العملاء المهتمين" in low:
        return {"tool": "list_leads", "args": {"limit": 20}}

    if "احصائيات" in low or "إحصائيات" in command_text:
        return {"tool": "stats", "args": {}}

    return {"tool": "unknown", "args": {}}


def _execute_add_course(app, admin_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "code": "VALIDATION", "message": "اسم الدورة مطلوب."}
    if args.get("price") is not None and args["price"] < 0:
        return {"ok": False, "code": "VALIDATION", "message": "السعر لا يمكن أن يكون سالباً."}
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            if _find_course(conn, name):
                return {"ok": False, "code": "DUPLICATE", "message": f"الدورة {name} موجودة مسبقاً."}
            conn.execute(
                """
                INSERT INTO academy_courses
                (name, lessons, duration_text, days_per_week, price, first_payment, start_date, topics)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    name, args.get("lessons"), args.get("duration_text"), args.get("days_per_week"),
                    args.get("price"), args.get("first_payment"), args.get("start_date"), args.get("topics"),
                ),
            )
            conn.commit()
            after = _snapshot_course(conn, name)
        finally:
            conn.close()
    _history(app, admin_id, "course", name, "create", None, after)
    return {"ok": True, "code": "DONE", "message": f"تمت إضافة دورة {name} وتسجيل بياناتها."}


def _execute_update_course(app, admin_id: str, args: dict) -> dict:
    name = args.get("name")
    updates = {k: v for k, v in args.items() if k != "name" and v is not None}
    if not name or not updates:
        return {"ok": False, "code": "VALIDATION", "message": "حدد اسم الدورة والمعلومة التي تريد تعديلها."}
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            before = _snapshot_course(conn, name)
            if not before:
                return {"ok": False, "code": "NOT_FOUND", "message": f"ما لقيت دورة باسم {name}."}
            allowed = {"lessons", "duration_text", "days_per_week", "price", "first_payment", "start_date", "topics", "active"}
            updates = {k: v for k, v in updates.items() if k in allowed}
            if not updates:
                return {"ok": False, "code": "VALIDATION", "message": "ما في حقول صالحة للتعديل."}
            assignments = ", ".join(f"{k}=?" for k in updates) + ", updated_at=CURRENT_TIMESTAMP"
            conn.execute(
                f"UPDATE academy_courses SET {assignments} WHERE id=?",
                [*updates.values(), before["id"]],
            )
            conn.commit()
            after = _snapshot_course(conn, name)
        finally:
            conn.close()
    _history(app, admin_id, "course", name, "update", before, after)
    return {"ok": True, "code": "DONE", "message": f"تم تعديل دورة {name} وتسجيل النسخة السابقة للتراجع عند الحاجة."}


def _execute_add_batch(app, admin_id: str, args: dict) -> dict:
    course = args.get("course")
    start_date = _date(args.get("start_date"))
    if not course or not start_date:
        return {"ok": False, "code": "VALIDATION", "message": "اسم الدورة وتاريخ البدء بصيغة YYYY-MM-DD مطلوبان."}
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            row = _find_course(conn, course)
            if not row:
                return {"ok": False, "code": "NOT_FOUND", "message": f"ما لقيت دورة باسم {course}."}
            cur = conn.execute(
                "INSERT INTO course_batches(course_id,start_date,schedule_text,active) VALUES (?,?,?,1)",
                (row[0], start_date, args.get("schedule_text")),
            )
            batch_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    _history(app, admin_id, "batch", str(batch_id), "create", None, {"course": course, **args})
    return {"ok": True, "code": "DONE", "message": f"تمت إضافة موعد بدء {course} بتاريخ {start_date}."}


def _execute_set_info(app, admin_id: str, args: dict) -> dict:
    key = (args.get("key") or "").strip()
    value = (args.get("value") or "").strip()
    if not key or not value:
        return {"ok": False, "code": "VALIDATION", "message": "اكتب المعلومة بهذا الشكل: المفتاح = القيمة."}
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            before_row = conn.execute("SELECT info_value FROM academy_info WHERE info_key=?", (key,)).fetchone()
            before = {"key": key, "value": before_row[0]} if before_row else None
            conn.execute(
                "INSERT OR REPLACE INTO academy_info(info_key,info_value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()
    _history(app, admin_id, "info", key, "upsert", before, {"key": key, "value": value})
    return {"ok": True, "code": "DONE", "message": f"تم تحديث المعلومة: {key}."}


def _execute_add_offer(app, admin_id: str, args: dict) -> dict:
    starts_at, ends_at = _date(args.get("starts_at")), _date(args.get("ends_at"))
    if not args.get("title") or not args.get("description") or not starts_at or not ends_at:
        return {"ok": False, "code": "VALIDATION", "message": "العرض يحتاج عنوان ووصف وتاريخ بداية ونهاية بصيغة YYYY-MM-DD."}
    if starts_at > ends_at:
        return {"ok": False, "code": "VALIDATION", "message": "تاريخ البداية يجب أن يسبق تاريخ النهاية."}
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            cur = conn.execute(
                "INSERT INTO academy_offers(title,description,starts_at,ends_at,active) VALUES (?,?,?,?,1)",
                (args["title"], args["description"], starts_at, ends_at),
            )
            offer_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    _history(app, admin_id, "offer", str(offer_id), "create", None, args)
    return {"ok": True, "code": "DONE", "message": f"تمت إضافة العرض {args['title']} من {starts_at} إلى {ends_at}."}


def _confirm_required(app, sender_id: str, action: dict, message: str) -> dict:
    _pending_confirmation(app, sender_id, action)
    return {"ok": False, "code": "CONFIRM_REQUIRED", "message": message}


def confirm_admin_action(app, sender_id: str) -> dict:
    action = _pop_confirmation(app, sender_id)
    if not action:
        return {"ok": False, "code": "NO_PENDING", "message": "ما في أمر معلّق للتأكيد."}
    tool = action.get("tool")
    if tool == "delete_info":
        key = action["key"]
        with getattr(app, "DB_LOCK", EXTENSION_LOCK):
            conn = _conn(app)
            try:
                before = conn.execute("SELECT info_value FROM academy_info WHERE info_key=?", (key,)).fetchone()
                conn.execute("DELETE FROM academy_info WHERE info_key=?", (key,))
                conn.commit()
            finally:
                conn.close()
        if before:
            _history(app, sender_id, "info", key, "delete", {"key": key, "value": before[0]}, None)
        return {"ok": True, "code": "DONE", "message": f"تم حذف المعلومة {key}."}
    if tool == "delete_course":
        name = action["name"]
        with getattr(app, "DB_LOCK", EXTENSION_LOCK):
            conn = _conn(app)
            try:
                before = _snapshot_course(conn, name)
                if not before:
                    return {"ok": False, "code": "NOT_FOUND", "message": f"ما لقيت دورة باسم {name}."}
                conn.execute("UPDATE academy_courses SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (before["id"],))
                conn.commit()
                after = _snapshot_course(conn, name)
            finally:
                conn.close()
        _history(app, sender_id, "course", name, "deactivate", before, after)
        return {"ok": True, "code": "DONE", "message": f"تم تعطيل دورة {name}."}
    if tool == "rollback_course":
        return _rollback_course(app, sender_id, action["name"])
    return {"ok": False, "code": "UNKNOWN_CONFIRM", "message": "الأمر المعلّق غير معروف."}


def _rollback_course(app, admin_id: str, name: str) -> dict:
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            row = conn.execute(
                """
                SELECT id, before_json, after_json
                FROM academy_change_history
                WHERE entity_type='course' AND entity_key=? AND action='update'
                ORDER BY id DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
            if not row or not row[1]:
                return {"ok": False, "code": "NO_HISTORY", "message": f"لا يوجد تعديل محفوظ يمكن التراجع عنه لدورة {name}."}
            current = _snapshot_course(conn, name)
            before = json.loads(row[1])
            fields = ["lessons", "duration_text", "days_per_week", "price", "first_payment", "start_date", "topics", "active"]
            assignments, values = [], []
            for field in fields:
                if field in before:
                    assignments.append(f"{field}=?")
                    values.append(before[field])
            conn.execute(
                f"UPDATE academy_courses SET {', '.join(assignments)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [*values, current["id"]],
            )
            conn.commit()
            after = _snapshot_course(conn, name)
        finally:
            conn.close()
    _history(app, admin_id, "course", name, "rollback", current, after)
    return {"ok": True, "code": "DONE", "message": f"تم التراجع عن آخر تعديل لدورة {name}."}


def upsert_lead(app, sender_id: str, message: str):
    text = _normalize(message).lower()
    score = 0
    weights = {
        "سعر": 15, "السعر": 15, "كم": 5,
        "قسط": 12, "الدفعة": 12, "التثبيت": 20, "شام كاش": 25,
        "موعد": 12, "متى": 10, "تبدأ": 10, "تبدا": 10,
        "حجز": 15, "سجل": 20, "تسجيل": 20, "اريد": 5, "بدي": 5,
    }
    for token, weight in weights.items():
        if token in text:
            score += weight
    course_name = None
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            courses = conn.execute("SELECT name FROM academy_courses WHERE active=1").fetchall()
            for (name,) in courses:
                if name and name.lower() in text:
                    course_name = name
                    score += 15
                    break
            row = conn.execute("SELECT score, messages_count FROM customer_leads WHERE sender_id=?", (sender_id,)).fetchone()
            current_score = int(row[0]) if row else 0
            messages = int(row[1]) if row else 0
            new_score = min(100, max(current_score, score) + (5 if row else 0))
            stage = "hot" if new_score >= 70 else "warm" if new_score >= 40 else "cold"
            conn.execute(
                """
                INSERT INTO customer_leads(sender_id,score,stage,interested_course,last_message,messages_count,last_seen,updated_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                ON CONFLICT(sender_id) DO UPDATE SET
                    score=excluded.score,
                    stage=excluded.stage,
                    interested_course=COALESCE(excluded.interested_course,customer_leads.interested_course),
                    last_message=excluded.last_message,
                    messages_count=excluded.messages_count,
                    last_seen=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (sender_id, new_score, stage, course_name, message[:2000], messages + 1),
            )
            conn.commit()
        finally:
            conn.close()
    return get_lead(app, sender_id)


def get_lead(app, sender_id: str) -> Optional[dict]:
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            row = conn.execute(
                "SELECT sender_id,score,stage,interested_course,last_message,messages_count,last_seen FROM customer_leads WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    keys = ["sender_id", "score", "stage", "interested_course", "last_message", "messages_count", "last_seen"]
    return dict(zip(keys, row))


def list_leads(app, limit=20):
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            rows = conn.execute(
                "SELECT sender_id,score,stage,interested_course,last_message,last_seen FROM customer_leads ORDER BY score DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    keys = ["sender_id", "score", "stage", "interested_course", "last_message", "last_seen"]
    return [dict(zip(keys, row)) for row in rows]


def build_extension_knowledge(app) -> str:
    today = date.today().isoformat()
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            offers = conn.execute(
                "SELECT title,description,starts_at,ends_at FROM academy_offers WHERE active=1 AND starts_at<=? AND ends_at>=? ORDER BY starts_at",
                (today, today),
            ).fetchall()
        finally:
            conn.close()
    if not offers:
        return ""
    lines = ["\n=== العروض الحالية المفعلة ==="]
    for title, description, starts_at, ends_at in offers:
        lines.append(f"- {title}: {description} | من {starts_at} إلى {ends_at}")
    return "\n".join(lines)


def _stats(app) -> str:
    with getattr(app, "DB_LOCK", EXTENSION_LOCK):
        conn = _conn(app)
        try:
            messages = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            users = conn.execute("SELECT COUNT(DISTINCT sender_id) FROM conversations").fetchone()[0]
            hot = conn.execute("SELECT COUNT(*) FROM customer_leads WHERE stage='hot'").fetchone()[0]
            warm = conn.execute("SELECT COUNT(*) FROM customer_leads WHERE stage='warm'").fetchone()[0]
            offers = conn.execute("SELECT COUNT(*) FROM academy_offers WHERE active=1").fetchone()[0]
        finally:
            conn.close()
    return f"إحصائيات محدثة: {messages} رسالة، {users} مستخدم، {hot} عملاء ساخنين، {warm} دافئين، و{offers} عروض مفعلة."


def execute_structured(app, sender_id: str, command_text: str) -> dict:
    parsed = parse_admin_command(command_text)
    tool, args = parsed["tool"], parsed.get("args", {})
    if tool == "confirm":
        return confirm_admin_action(app, sender_id)
    if tool == "cancel":
        action = _pop_confirmation(app, sender_id)
        return {"ok": bool(action), "code": "CANCELLED", "message": "تم إلغاء الأمر المعلّق." if action else "ما في أمر معلّق."}
    if tool == "unknown":
        return {"ok": False, "handled": False, "code": "NOT_ADMIN_COMMAND", "message": ""}
    if tool == "invalid":
        return {"ok": False, "handled": True, "code": "VALIDATION", "message": parsed.get("reason", "صيغة الأمر غير صحيحة.")}
    if parsed.get("confirmation_required"):
        if tool == "delete_course":
            return _confirm_required(app, sender_id, {"tool": tool, **args}, f"سيتم تعطيل دورة {args.get('name')}. اكتب نعم للتأكيد أو لا للإلغاء.")
        if tool == "delete_info":
            return _confirm_required(app, sender_id, {"tool": tool, **args}, f"سيتم حذف المعلومة {args.get('key')}. اكتب نعم للتأكيد أو لا للإلغاء.")
    if tool == "add_course":
        return _execute_add_course(app, sender_id, args)
    if tool == "update_course":
        return _execute_update_course(app, sender_id, args)
    if tool == "add_batch":
        return _execute_add_batch(app, sender_id, args)
    if tool == "set_info":
        return _execute_set_info(app, sender_id, args)
    if tool == "add_offer":
        return _execute_add_offer(app, sender_id, args)
    if tool == "rollback_course":
        return _rollback_course(app, sender_id, args.get("name"))
    if tool == "list_leads":
        leads = list_leads(app, args.get("limit", 20))
        if not leads:
            return {"ok": True, "handled": True, "code": "DONE", "message": "لا يوجد عملاء مهتمون مسجلون بعد."}
        msg = "أعلى العملاء اهتماماً:\n" + "\n".join(
            f"- {x['sender_id']} | {x['score']}/100 | {x['stage']} | {x['interested_course'] or 'غير محدد'}"
            for x in leads
        )
        return {"ok": True, "handled": True, "code": "DONE", "message": msg}
    if tool == "stats":
        return {"ok": True, "handled": True, "code": "DONE", "message": _stats(app)}
    return {"ok": False, "handled": False, "code": "NOT_ADMIN_COMMAND", "message": ""}


def route_admin_command(app, sender_id: str, command_text: str) -> dict:
    parsed = parse_admin_command(command_text)
    if parsed["tool"] == "unknown":
        return {"handled": False}
    result = execute_structured(app, sender_id, command_text)
    result["handled"] = True
    return result


def _wrap_admin_execute(app, original):
    def wrapped(sender_id, command_text):
        result = route_admin_command(app, sender_id, command_text)
        if result.get("handled"):
            return result
        return original(sender_id, command_text)
    return wrapped


def _wrap_ai(app, original):
    def wrapped(sender_id, user_message, intent=None, is_admin=False, message_id=None):
        try:
            if not is_admin:
                upsert_lead(app, sender_id, user_message)
        except Exception as exc:
            print(f"[LEAD] scoring failed: {exc}", flush=True)
        return original(sender_id, user_message, intent=intent, is_admin=is_admin, message_id=message_id)
    return wrapped


def apply_patch(app_module=None):
    """Patch the already-imported app exactly once. Safe to call repeatedly."""
    if app_module is None:
        import app as app_module
    if getattr(app_module, PATCH_MARKER, False):
        return app_module
    init_extension_schema(app_module)

    original_admin = getattr(app_module, "admin_execute", None)
    if callable(original_admin):
        app_module.admin_execute = _wrap_admin_execute(app_module, original_admin)

    original_ai = getattr(app_module, "generate_ai_reply", None)
    if callable(original_ai):
        app_module.generate_ai_reply = _wrap_ai(app_module, original_ai)

    original_knowledge = getattr(app_module, "build_dynamic_academy_knowledge", None)
    if callable(original_knowledge):
        def knowledge_wrapper(*args, **kwargs):
            base = original_knowledge(*args, **kwargs)
            return base + build_extension_knowledge(app_module)
        app_module.build_dynamic_academy_knowledge = knowledge_wrapper

    setattr(app_module, PATCH_MARKER, True)
    print("[ADMIN_RUNTIME] structured admin/CRM extension enabled", flush=True)
    return app_module
