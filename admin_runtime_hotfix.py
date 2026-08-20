"""Compatibility fixes layered over admin_runtime without modifying the legacy app."""
from __future__ import annotations

import re

import admin_runtime as base

_ORIGINAL_PARSE = base.parse_admin_command


def _normalize(text: str) -> str:
    value = (text or '').strip()
    value = re.sub(r'[\u0610-\u061A\u064B-\u065F\u0670]', '', value)
    value = value.replace('،', ',').replace('؛', ';')
    return re.sub(r'\s+', ' ', value)


def _parse_structured_update(text: str):
    low = _normalize(text).lower()
    if not ('غير' in low or 'عدل' in low or 'تعديل' in low) or 'دورة' not in low:
        return None
    match = re.search(
        r'دورة\s+(.+?)(?=\s+(?:الى|إلى|السعر|سعر|الدفعة|دفعة|الدروس|عدد الدروس|المحاور|محاور|مدة|تبدأ|تبدا)|[,;]|$)',
        text,
        flags=re.I,
    )
    if not match:
        return None
    args = {'name': match.group(1).strip(' .،,')}
    patterns = {
        'price': r'(?:السعر|سعر)(?:\s+دورة\s+[^,;]+?)?\s*(?:إلى|الى|=|:)??\s*([0-9][0-9,]*)',
        'first_payment': r'(?:الدفعة الأولى|الدفعة الاولى|دفعة أولى|دفعة اولى)\s*(?:إلى|الى|=|:)??\s*([0-9][0-9,]*)',
        'lessons': r'(?:عدد الدروس|الدروس|درس)\s*(?:إلى|الى|=|:)??\s*([0-9][0-9,]*)',
        'days_per_week': r'(?:أيام بالأسبوع|ايام بالأسبوع|ايام بالاسبوع)\s*(?:إلى|الى|=|:)??\s*([0-9][0-9,]*)',
    }
    for field, pattern in patterns.items():
        found = re.search(pattern, text, flags=re.I)
        if found:
            args[field] = int(re.sub(r'[^0-9]', '', found.group(1)))
    duration = re.search(
        r'(?:مدة الدرس|مدة)\s*(?:إلى|الى|=|:)?\s*([^,;]+?)(?=\s+و(?:غير|عدل)|,|;|$)',
        text,
        flags=re.I,
    )
    if duration:
        args['duration_text'] = duration.group(1).strip()
    topics = re.search(
        r'(?:المحاور|محاور)\s*(?:إلى|الى|=|:)?\s*([^,;]+?)(?=\s+و(?:غير|عدل)|,|;|$)',
        text,
        flags=re.I,
    )
    if topics:
        args['topics'] = topics.group(1).strip()
    date_match = re.search(r'(20\d{2}[-/]\d{1,2}[-/]\d{1,2})', text)
    if date_match:
        args['start_date'] = base._date(date_match.group(1))
    return {'tool': 'update_course', 'args': args}


def parse_admin_command(command_text: str):
    structured = _parse_structured_update(command_text)
    return structured if structured else _ORIGINAL_PARSE(command_text)


def upsert_lead(app, sender_id: str, message: str):
    """Same lead model as the base extension, with stronger explicit booking/payment signals."""
    text = _normalize(message).lower()
    score = 0
    weights = {
        'سعر': 15, 'السعر': 15, 'كم': 5, 'قسط': 12, 'الدفعة': 12,
        'التثبيت': 20, 'ثبت': 20, 'شام كاش': 25, 'موعد': 12, 'متى': 10,
        'تبدأ': 10, 'تبدا': 10, 'حجز': 15, 'حجزت': 20, 'سجل': 20,
        'تسجيل': 20, 'اريد': 5, 'بدي': 5,
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


def _wrap_admin_execute(app, original):
    def wrapped(sender_id, command_text):
        parsed = parse_admin_command(command_text)
        if parsed.get('tool') != 'unknown':
            result = base.execute_structured(app, sender_id, command_text)
            result['handled'] = True
            return result
        return original(sender_id, command_text)
    return wrapped


def apply_patch(app_module=None):
    base._normalize = _normalize
    base.parse_admin_command = parse_admin_command
    base.upsert_lead = upsert_lead
    if app_module is None:
        import app as app_module
    if getattr(app_module, base.PATCH_MARKER, False):
        return app_module
    base.init_extension_schema(app_module)
    original_admin = getattr(app_module, 'admin_execute', None)
    if callable(original_admin):
        app_module.admin_execute = _wrap_admin_execute(app_module, original_admin)
    original_ai = getattr(app_module, 'generate_ai_reply', None)
    if callable(original_ai):
        app_module.generate_ai_reply = base._wrap_ai(app_module, original_ai)
    original_knowledge = getattr(app_module, 'build_dynamic_academy_knowledge', None)
    if callable(original_knowledge):
        def knowledge_wrapper(*args, **kwargs):
            return original_knowledge(*args, **kwargs) + base.build_extension_knowledge(app_module)
        app_module.build_dynamic_academy_knowledge = knowledge_wrapper
    setattr(app_module, base.PATCH_MARKER, True)
    print('[ADMIN_RUNTIME] structured admin/CRM extension enabled', flush=True)
    return app_module
