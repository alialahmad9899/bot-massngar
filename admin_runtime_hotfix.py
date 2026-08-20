"""Compatibility fixes layered over admin_runtime without modifying the legacy app."""
from __future__ import annotations

import re

import admin_runtime as base


def _normalize(text: str) -> str:
    value = (text or '').strip()
    # Remove Arabic tashkeel/harakat, including shadda in غيّر/عدّل, while preserving letters such as أ in course names.
    value = re.sub(r'[\u0610-\u061A\u064B-\u065F\u0670]', '', value)
    value = value.replace('،', ',').replace('؛', ';')
    return re.sub(r'\s+', ' ', value)


def upsert_lead(app, sender_id: str, message: str):
    """Same lead model as the base extension, with stronger explicit booking/payment signals."""
    text = _normalize(message).lower()
    score = 0
    weights = {
        'سعر': 15,
        'السعر': 15,
        'كم': 5,
        'قسط': 12,
        'الدفعة': 12,
        'التثبيت': 20,
        'ثبت': 20,
        'شام كاش': 25,
        'موعد': 12,
        'متى': 10,
        'تبدأ': 10,
        'تبدا': 10,
        'حجز': 15,
        'حجزت': 20,
        'سجل': 20,
        'تسجيل': 20,
        'اريد': 5,
        'بدي': 5,
    }
    for token, weight in weights.items():
        if token in text:
            score += weight

    with getattr(app, 'DB_LOCK', base.EXTENSION_LOCK):
        conn = app.get_db_connection()
        try:
            course_name = None
            for (name,) in conn.execute('SELECT name FROM academy_courses WHERE active=1').fetchall():
                if name and name.lower() in text:
                    course_name = name
                    score += 15
                    break
            row = conn.execute(
                'SELECT score, messages_count FROM customer_leads WHERE sender_id=?',
                (sender_id,),
            ).fetchone()
            current_score = int(row[0]) if row else 0
            messages = int(row[1]) if row else 0
            new_score = min(100, max(current_score, score) + (5 if row else 0))
            stage = 'hot' if new_score >= 70 else 'warm' if new_score >= 40 else 'cold'
            conn.execute(
                '''
                INSERT INTO customer_leads
                    (sender_id, score, stage, interested_course, last_message, messages_count, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(sender_id) DO UPDATE SET
                    score=excluded.score,
                    stage=excluded.stage,
                    interested_course=COALESCE(excluded.interested_course, customer_leads.interested_course),
                    last_message=excluded.last_message,
                    messages_count=excluded.messages_count,
                    last_seen=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                ''',
                (sender_id, new_score, stage, course_name, message[:2000], messages + 1),
            )
            conn.commit()
        finally:
            conn.close()
    return base.get_lead(app, sender_id)


def apply_patch(app_module=None):
    base._normalize = _normalize
    base.upsert_lead = upsert_lead
    return base.apply_patch(app_module)
