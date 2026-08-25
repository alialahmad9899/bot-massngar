"""Canonical admin command adapter over the existing admin implementation."""
from __future__ import annotations

import re
from typing import Any

import admin_runtime as base


def _normalize(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670]", "", value)
    for a, b in {"أ": "ا", "إ": "ا", "آ": "ا", "،": ",", "؛": ";"}.items():
        value = value.replace(a, b)
    return re.sub(r"\s+", " ", value)


def _parse_update_course(text: str):
    low = _normalize(text).lower()
    if not ("غير" in low or "عدل" in low or "تعديل" in low) or "دورة" not in low:
        return None
    match = re.search(
        r"دورة\s+(.+?)(?=\s+(?:الى|إلى|السعر|سعر|الدفعة|دفعة|الدروس|عدد الدروس|المحاور|محاور|مدة|تبدأ|تبدا)|[,;]|$)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    args = {"name": match.group(1).strip(" .،,")}
    patterns = {
        "price": r"(?:السعر|سعر)(?:\s+دورة\s+[^,;]+?)?\s*(?:إلى|الى|=|:)?\s*([0-9][0-9,]*)",
        "first_payment": r"(?:الدفعة الأولى|الدفعة الاولى|دفعة أولى|دفعة اولى)\s*(?:إلى|الى|=|:)?\s*([0-9][0-9,]*)",
        "lessons": r"(?:عدد الدروس|الدروس|درس)\s*(?:إلى|الى|=|:)?\s*([0-9][0-9,]*)",
        "days_per_week": r"(?:أيام بالأسبوع|ايام بالأسبوع|ايام بالاسبوع)\s*(?:إلى|الى|=|:)?\s*([0-9][0-9,]*)",
    }
    for field, pattern in patterns.items():
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if found:
            args[field] = int(re.sub(r"[^0-9]", "", found.group(1)))
    duration = re.search(r"(?:مدة الدرس|مدة)\s*(?:إلى|الى|=|:)?\s*([^,;]+?)(?=\s+و(?:غير|عدل)|,|;|$)", text, flags=re.IGNORECASE)
    if duration:
        args["duration_text"] = duration.group(1).strip()
    topics = re.search(r"(?:المحاور|محاور)\s*(?:إلى|الى|=|:)?\s*([^,;]+?)(?=\s+و(?:غير|عدل)|,|;|$)", text, flags=re.IGNORECASE)
    if topics:
        args["topics"] = topics.group(1).strip()
    date_match = re.search(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})", text)
    if date_match:
        args["start_date"] = base._date(date_match.group(1))
    return {"tool": "update_course", "args": args}


def parse_admin_command(command_text: str) -> dict:
    normalized = _normalize(command_text)
    low = normalized.lower()
    if "تراجع عن" in low or "رجع" in low:
        m = re.search(r"دورة\s+(.+)$", normalized)
        name = m.group(1).strip() if m else None
        return {"tool": "rollback_course", "args": {"name": name}}
    structured = _parse_update_course(normalized)
    if structured:
        return structured
    return base.parse_admin_command(normalized)


def execute_structured(app: Any, sender_id: str, command_text: str) -> dict:
    parsed = parse_admin_command(command_text)
    tool = parsed.get("tool")
    args = parsed.get("args", {})
    if tool == "update_course":
        return base._execute_update_course(app, sender_id, args)
    if tool == "rollback_course":
        return base._rollback_course(app, sender_id, args.get("name"))
    if tool == "unknown":
        return {"ok": False, "handled": False, "code": "NOT_ADMIN_COMMAND", "message": ""}
    return base.execute_structured(app, sender_id, command_text)


def route_admin_command(app: Any, sender_id: str, command_text: str) -> dict:
    parsed = parse_admin_command(command_text)
    if parsed.get("tool") == "unknown":
        return {"handled": False}
    result = execute_structured(app, sender_id, command_text)
    result["handled"] = True
    return result


def upsert_lead(app: Any, sender_id: str, message: str):
    text = _normalize(message).lower()
    weights = {
        "سعر": 15, "السعر": 15, "كم": 5, "قسط": 12, "الدفعة": 12,
        "التثبيت": 20, "ثبت": 20, "شام كاش": 25, "موعد": 12, "متى": 10,
        "تبدأ": 10, "تبدا": 10, "حجز": 15, "حجزت": 20, "سجل": 20,
        "تسجيل": 20, "اريد": 5, "بدي": 5,
    }
    score = sum(weight for token, weight in weights.items() if token in text)
    course_name = None
    with getattr(app, "DB_LOCK", base.EXTENSION_LOCK):
        conn = app.get_db_connection()
        try:
            for (name,) in conn.execute("SELECT name FROM academy_courses WHERE active=1").fetchall():
                if name and _normalize(name).lower() in text:
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
    return base.get_lead(app, sender_id)


def list_leads(app: Any, limit=20):
    return base.list_leads(app, limit=limit)


def get_lead(app: Any, sender_id: str):
    return base.get_lead(app, sender_id)


def init_extension_schema(app: Any):
    return base.init_extension_schema(app)
