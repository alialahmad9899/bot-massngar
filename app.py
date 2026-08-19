import hashlib
import hmac
import json
import os
import queue
import sqlite3
import threading
import time
from typing import Optional

import requests
from flask import Flask, request
from google import genai


app = Flask(__name__)

# ========================================================
# 🔐 Required secrets / configuration
# ========================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
APP_SECRET = os.environ.get("APP_SECRET")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

_REQUIRED_ENV = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "PAGE_ACCESS_TOKEN": PAGE_ACCESS_TOKEN,
    "VERIFY_TOKEN": VERIFY_TOKEN,
    "APP_SECRET": APP_SECRET,
    "ADMIN_PASSWORD": ADMIN_PASSWORD,
}
_missing_env = [name for name, value in _REQUIRED_ENV.items() if not value]
if _missing_env:
    raise RuntimeError(
        "CRITICAL ERROR: Missing required environment variables: "
        + ", ".join(_missing_env)
    )

DB_PATH = os.environ.get("DB_PATH", "academy_bot.db")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Render 512 MB: keep memory bounded and concurrency intentionally small.
WORKER_COUNT = max(1, min(int(os.environ.get("BOT_WORKERS", "2")), 2))
MESSAGE_QUEUE_MAXSIZE = max(20, min(int(os.environ.get("MESSAGE_QUEUE_MAXSIZE", "100")), 200))
USER_RATE_LIMIT_SECONDS = float(os.environ.get("USER_RATE_LIMIT_SECONDS", "1.0"))
DEDUP_TTL_SECONDS = 24 * 60 * 60
ADMIN_LOCKOUT_SECONDS = 15 * 60
ADMIN_MAX_ATTEMPTS = 3
EVENT_MAX_RETRIES = 5
EVENT_STALE_PROCESSING_SECONDS = 10 * 60
FACEBOOK_TIMEOUT_SECONDS = 8
FACEBOOK_RETRY_ATTEMPTS = 3
AUTO_START_WORKERS = os.environ.get("BOT_AUTO_START_WORKERS", "1") == "1"

# ========================================================
# 🧠 Gemini client
# ========================================================
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ========================================================
# 🛡️ In-memory, bounded/thread-safe state
# ========================================================
DB_LOCK = threading.RLock()
PROCESSED_MESSAGES = {}  # {message_id: timestamp}
PROCESSED_MESSAGES_LOCK = threading.Lock()
ADMIN_ATTEMPTS = {}  # {sender_id: (attempts_count, lock_expiry_time)}
ADMIN_ATTEMPTS_LOCK = threading.Lock()
LAST_PROCESSED_TIME = {}
LAST_PROCESSED_LOCK = threading.Lock()

# Stripe locks avoid an unbounded dict of per-user Lock objects.
USER_LOCK_COUNT = 32
USER_LOCKS = [threading.Lock() for _ in range(USER_LOCK_COUNT)]

MESSAGE_QUEUE = queue.Queue(maxsize=MESSAGE_QUEUE_MAXSIZE)
STOP_EVENT = threading.Event()
WORKERS = []
FACEBOOK_SESSION = requests.Session()


# ========================================================
# 🗄️ SQLite / durable local inbox
# ========================================================
def get_db_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10.0,
        check_same_thread=False,
    )
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_column(conn, table_name: str, column_name: str, column_type: str):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def init_db():
    with DB_LOCK:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    intent TEXT,
                    message_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Backward-compatible migration for the older database schema.
            _ensure_column(conn, "conversations", "intent", "TEXT")
            _ensure_column(conn, "conversations", "message_id", "TEXT")

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS human_handover (
                    sender_id TEXT PRIMARY KEY,
                    is_paused INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    sender_id TEXT PRIMARY KEY,
                    is_admin INTEGER DEFAULT 0,
                    awaiting_password INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Durable local inbox. Render restarts still require a persistent disk
            # for these rows to survive process/instance replacement.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    sender_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retries INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    claimed_at REAL,
                    response_text TEXT,
                    response_quick_replies TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT
                )
                """
            )
            _ensure_column(conn, "webhook_events", "response_text", "TEXT")
            _ensure_column(conn, "webhook_events", "response_quick_replies", "TEXT")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status, available_at, id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_sender_id ON conversations(sender_id, id)"
            )

            # Admin-managed knowledge and operational tables. These let the verified
            # manager change academy data without editing Python code.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS academy_courses (
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS course_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    schedule_text TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(course_id) REFERENCES academy_courses(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS academy_info (
                    info_key TEXT PRIMARY KEY,
                    info_value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    command_text TEXT NOT NULL,
                    result_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_pending_actions (
                    sender_id TEXT PRIMARY KEY,
                    action_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_course_batches_course ON course_batches(course_id, start_date)"
            )
            conn.commit()
        finally:
            conn.close()


def reset_stale_processing_events():
    cutoff = time.time() - EVENT_STALE_PROCESSING_SECONDS
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                UPDATE webhook_events
                SET status='pending', claimed_at=NULL, available_at=?
                WHERE status='processing' AND claimed_at IS NOT NULL AND claimed_at < ?
                """,
                (time.time(), cutoff),
            )
            conn.commit()
        finally:
            conn.close()


def enqueue_webhook_event(sender_id: str, message_id: Optional[str], payload: dict) -> Optional[int]:
    """Persist first, enqueue second. This prevents loss when RAM queue is full."""
    raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with DB_LOCK:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO webhook_events
                    (message_id, sender_id, payload, status, available_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (message_id, sender_id, raw_payload, time.time()),
            )
            if cursor.rowcount == 0 and message_id:
                row = cursor.execute(
                    "SELECT id FROM webhook_events WHERE message_id=?",
                    (message_id,),
                ).fetchone()
            else:
                row = (cursor.lastrowid,)
            conn.commit()
            event_id = int(row[0]) if row and row[0] else None
        finally:
            conn.close()

    if event_id is not None:
        print(f"[QUEUE] persisted event_id={event_id} sender={sender_id} mid={message_id}", flush=True)
        try:
            MESSAGE_QUEUE.put_nowait(event_id)
            print(f"[QUEUE] enqueued event_id={event_id} size={MESSAGE_QUEUE.qsize()}", flush=True)
        except queue.Full:
            # Revert to pending so the DB-backed poller can pick it up later.
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    conn.execute(
                        "UPDATE webhook_events SET status='pending' WHERE id=? AND status='queued'",
                        (event_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
            print(f"[QUEUE] RAM queue full; event_id={event_id} left in DB for poller", flush=True)
    return event_id


def claim_next_pending_event() -> Optional[int]:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id FROM webhook_events
                WHERE status IN ('pending', 'queued') AND available_at <= ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            event_id = int(row[0])
            updated = conn.execute(
                """
                UPDATE webhook_events
                SET status='processing', claimed_at=?
                WHERE id=? AND status IN ('pending', 'queued')
                """,
                (time.time(), event_id),
            ).rowcount
            conn.commit()
            return event_id if updated else None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def load_event(event_id: int):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                """
                SELECT id, sender_id, payload, retries, response_text, response_quick_replies
                FROM webhook_events
                WHERE id=?
                """,
                (event_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "sender_id": row[1],
                "payload": json.loads(row[2]),
                "retries": row[3],
                "response_text": row[4],
                "response_quick_replies": json.loads(row[5]) if row[5] else None,
            }
        finally:
            conn.close()


def claim_queued_or_pending_event(event_id: int) -> bool:
    """Atomically claim an event already queued in RAM or left pending in DB."""
    with DB_LOCK:
        conn = get_db_connection()
        try:
            claimed = bool(
                conn.execute(
                    """
                    UPDATE webhook_events
                    SET status='processing', claimed_at=?
                    WHERE id=? AND status IN ('queued', 'pending')
                    """,
                    (time.time(), event_id),
                ).rowcount
            )
            conn.commit()
            return claimed
        finally:
            conn.close()


def save_event_response(event_id: int, response_text: str, quick_replies=None):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                UPDATE webhook_events
                SET response_text=?, response_quick_replies=?
                WHERE id=?
                """,
                (
                    response_text,
                    json.dumps(quick_replies, ensure_ascii=False) if quick_replies else None,
                    event_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def mark_event_completed(event_id: int):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE webhook_events SET status='completed', claimed_at=NULL, last_error=NULL WHERE id=?",
                (event_id,),
            )
            conn.commit()
        finally:
            conn.close()


def mark_event_failed(event_id: int, error_message: str, retryable: bool = True):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT retries FROM webhook_events WHERE id=?",
                (event_id,),
            ).fetchone()
            retries = int(row[0]) if row else 0
            if retryable and retries + 1 < EVENT_MAX_RETRIES:
                next_retry = retries + 1
                delay = min(60, 2 ** min(next_retry, 6))
                conn.execute(
                    """
                    UPDATE webhook_events
                    SET status='pending', retries=?, claimed_at=NULL, available_at=?, last_error=?
                    WHERE id=?
                    """,
                    (next_retry, time.time() + delay, error_message[:1000], event_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE webhook_events
                    SET status='failed', retries=?, claimed_at=NULL, last_error=?
                    WHERE id=?
                    """,
                    (retries + (1 if retryable else 0), error_message[:1000], event_id),
                )
            conn.commit()
        finally:
            conn.close()


# ========================================================
# 💬 Conversation storage
# ========================================================
def get_user_history_db(sender_id, limit=12):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT role, content, intent FROM (
                    SELECT role, content, intent, id
                    FROM conversations
                    WHERE sender_id=?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (sender_id, limit),
            ).fetchall()
        finally:
            conn.close()

    history = []
    for role, content, intent in rows:
        text = content
        if intent:
            text = f"[نية محددة من زر تفاعلي: {intent}]\n{text}"
        history.append({"role": role, "parts": [{"text": text}]})
    return history


def save_message_db(sender_id, role, content, intent=None, message_id=None):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO conversations (sender_id, role, content, intent, message_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sender_id, role, content, intent, message_id),
            )
            conn.commit()
        finally:
            conn.close()


def set_handover_status(sender_id, status=1):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO human_handover (sender_id, is_paused)
                VALUES (?, ?)
                ON CONFLICT(sender_id) DO UPDATE SET
                    is_paused=excluded.is_paused,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (sender_id, status),
            )
            conn.commit()
        finally:
            conn.close()


def is_user_paused(sender_id):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT is_paused FROM human_handover WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
        finally:
            conn.close()
    return bool(row[0]) if row else False


def set_admin_status(sender_id, is_admin=1, awaiting_password=0):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO admin_users (sender_id, is_admin, awaiting_password)
                VALUES (?, ?, ?)
                ON CONFLICT(sender_id) DO UPDATE SET
                    is_admin=excluded.is_admin,
                    awaiting_password=excluded.awaiting_password,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (sender_id, is_admin, awaiting_password),
            )
            conn.commit()
        finally:
            conn.close()


def get_admin_status(sender_id):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT is_admin, awaiting_password FROM admin_users WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
        finally:
            conn.close()
    if row:
        return {"is_admin": bool(row[0]), "awaiting_password": bool(row[1])}
    return {"is_admin": False, "awaiting_password": False}



# ========================================================
# 👑 Admin Command Center
# ========================================================
ADMIN_CONFIRM_TTL_SECONDS = 120


def _admin_audit(sender_id, action, command_text, result_text):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO admin_audit_log(sender_id, action, command_text, result_text) VALUES (?, ?, ?, ?)",
                (sender_id, action, command_text, result_text[:2000] if result_text else None),
            )
            conn.commit()
        finally:
            conn.close()


def _admin_set_pending(sender_id, action):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO admin_pending_actions(sender_id, action_json, expires_at) VALUES (?, ?, ?)",
                (sender_id, json.dumps(action, ensure_ascii=False), time.time() + ADMIN_CONFIRM_TTL_SECONDS),
            )
            conn.commit()
        finally:
            conn.close()


def _admin_pop_pending(sender_id):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT action_json, expires_at FROM admin_pending_actions WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
            conn.execute("DELETE FROM admin_pending_actions WHERE sender_id=?", (sender_id,))
            conn.commit()
        finally:
            conn.close()
    if not row:
        return None
    if time.time() > float(row[1]):
        return None
    return json.loads(row[0])


def _money(value):
    if value is None:
        return None
    digits = ''.join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def _normalize_admin_text(text):
    replacements = {
        'أ': 'ا', 'إ': 'ا', 'آ': 'ا',
        '،': ',', '؛': ';',
    }
    out = (text or '').strip()
    for a, b in replacements.items():
        out = out.replace(a, b)
    return out


def _extract_field(text, labels):
    import re
    label = '|'.join(re.escape(x) for x in labels)
    match = re.search(rf'(?:{label})\s*[:=]?\s*([^,;\n]+)', text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_int_field(text, labels):
    import re
    # Supports both: "السعر 900000" and "900000 ل.س" for price-like labels,
    # plus "16 درس" / "3 أيام" where the number precedes the label.
    label_pattern = '|'.join(re.escape(x) for x in labels)
    patterns = [
        rf'(?:{label_pattern})\s*[:=]?\s*([0-9][0-9,]*)',
        rf'([0-9][0-9,]*)\s*(?:{label_pattern})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _money(match.group(1))
    return None

def _extract_date(text):
    import re
    match = re.search(r'(20\d{2}[-/]\d{1,2}[-/]\d{1,2})', text)
    if not match:
        return None
    return match.group(1).replace('/', '-')


def _extract_course_name(text):
    import re
    patterns = [
        r'(?:دورة|course)\s+(.+?)(?=,|،|؛|;|:| السعر|السعر| عدد| العدد| الدفعة|الدفعة| تبدأ|تبدأ| تبدا|تبدا| المحاور|المحاور| 20\d{2}|$)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip(' .،,')
    return None


def _find_course(conn, name):
    if not name:
        return None
    return conn.execute(
        "SELECT * FROM academy_courses WHERE lower(name)=lower(?) LIMIT 1", (name,)
    ).fetchone()


def _course_dict(row):
    if not row:
        return None
    cols = ['id','name','lessons','duration_text','days_per_week','price','first_payment','start_date','topics','active','created_at','updated_at']
    return dict(zip(cols, row))


def admin_list_courses(include_inactive=False):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            sql = "SELECT * FROM academy_courses"
            if not include_inactive:
                sql += " WHERE active=1"
            sql += " ORDER BY id"
            return [_course_dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()


def admin_list_batches():
    with DB_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT b.id, c.name, b.start_date, b.schedule_text, b.active
                FROM course_batches b JOIN academy_courses c ON c.id=b.course_id
                WHERE b.active=1 ORDER BY b.start_date, b.id
                """
            ).fetchall()
            return [
                {'id': r[0], 'course_name': r[1], 'start_date': r[2], 'schedule_text': r[3], 'active': bool(r[4])}
                for r in rows
            ]
        finally:
            conn.close()


def get_dynamic_knowledge(key):
    with DB_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT info_value FROM academy_info WHERE info_key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


def _all_dynamic_knowledge():
    with DB_LOCK:
        conn = get_db_connection()
        try:
            return conn.execute("SELECT info_key, info_value FROM academy_info WHERE info_key <> '__legacy_courses_seeded' ORDER BY info_key").fetchall()
        finally:
            conn.close()


def _seed_legacy_courses_if_empty():
    # Seed the original hard-coded catalogue exactly once for existing deployments.
    # After the manager edits/deletes courses, we never resurrect the old catalogue.
    with DB_LOCK:
        conn = get_db_connection()
        try:
            marker = conn.execute(
                "SELECT info_value FROM academy_info WHERE info_key='__legacy_courses_seeded'"
            ).fetchone()
            if marker:
                return
            existing = conn.execute("SELECT 1 FROM academy_courses LIMIT 1").fetchone()
        finally:
            conn.close()
    if existing:
        with DB_LOCK:
            conn = get_db_connection()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO academy_info(info_key,info_value,updated_at) VALUES ('__legacy_courses_seeded','1',CURRENT_TIMESTAMP)"
                )
                conn.commit()
            finally:
                conn.close()
        return
    legacy = [
        ('تنظيف وعناية بالبشرة',14,'ساعة لساعتين',3,1500000,400000,'','تشخيص البشرة، أجهزة الهيدرافشيال والديرمابن والتقشير الكريستالي، المستحضرات، المساج والتعقيم'),
        ('حلاقة نسائية',20,'ساعة لساعتين',3,950000,200000,'','قصات حديثة، سيشوار وفير، صبغ وتخصيل والعناية بالشعر'),
        ('شنيون وتسريحات',14,'ساعة لساعتين',3,850000,200000,'','تسريحات عرائس 3D، تسريحات مرفوعة ومنسدلة، تثبيت الإكسسوارات والطرحة'),
        ('جل أظافر Gel Nails',14,'ساعة لساعتين',3,750000,200000,'','تقنيات عادية وروسية، تمديد وتكثيف (اكستنشن وفايبر)، رتوش، فرنش ورسم، تطبيق على موديل'),
        ('حلاقة رجالية',20,'ساعة لساعتين',3,750000,200000,'','قصات حديثة، حلاقة وتحديد اللحية، استخدام الموس، سيشوار، صبغة وحناء'),
        ('إكستنيشن رموش & Lash Lifting',12,'ساعة لساعتين',3,700000,200000,'','تركيب وعزل الرموش، رفع وتثبيت الرموش، والعناية بعد الجلسة'),
        ('ميك أب احترافي Make-up',14,'ساعة لساعتين',3,700000,200000,'','تهيئة البشرة، فونديشن وكوركتر، آيلاينر وإيشادو، كونتور، تركيب رموش، لوكات ناعمة وسهرات'),
    ]
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO academy_courses(name, lessons, duration_text, days_per_week, price, first_payment, start_date, topics) VALUES (?,?,?,?,?,?,?,?)",
                legacy,
            )
            conn.execute(
                "INSERT OR REPLACE INTO academy_info(info_key,info_value,updated_at) VALUES ('__legacy_courses_seeded','1',CURRENT_TIMESTAMP)"
            )
            conn.commit()
        finally:
            conn.close()


def _format_courses(courses):
    if not courses:
        return 'ما في دورات مسجلة حالياً.'
    lines = ['الدورات الحالية:']
    for c in courses:
        price = f"{c['price']:,} ل.س" if c['price'] is not None else 'غير محدد'
        deposit = f"{c['first_payment']:,} ل.س" if c['first_payment'] is not None else 'غير محددة'
        start = c['start_date'] or 'غير محدد'
        lines.append(f"- {c['name']} | {c['lessons'] or '?'} درس | السعر {price} | الدفعة الأولى {deposit} | البداية {start}")
    return '\n'.join(lines)


def _format_batches(batches):
    if not batches:
        return 'ما في مواعيد بدء مسجلة حالياً.'
    return 'مواعيد البدء:\n' + '\n'.join(
        f"- {b['course_name']}: {b['start_date']}" + (f" ({b['schedule_text']})" if b['schedule_text'] else '')
        for b in batches
    )


def _set_course(conn, name, fields):
    existing = _find_course(conn, name)
    if not existing:
        return False, f'ما لقيت دورة باسم: {name}'
    allowed = ['lessons','duration_text','days_per_week','price','first_payment','start_date','topics','active']
    updates = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
    if not updates:
        return False, 'ما في أي معلومة جديدة للتعديل.'
    assignments = ', '.join(f'{k}=?' for k, _ in updates) + ', updated_at=CURRENT_TIMESTAMP'
    conn.execute(f'UPDATE academy_courses SET {assignments} WHERE id=?', [v for _, v in updates] + [existing[0]])
    return True, f'تم تعديل دورة {name}.'


def admin_execute(sender_id, command_text):
    """Deterministic, audited admin command router. Returns {ok, message, code?}."""
    raw = (command_text or '').strip()
    text = _normalize_admin_text(raw)
    low = text.lower()

    # Confirmation of a destructive/sensitive action.
    if low in {'نعم', 'اكد', 'اكد التنفيذ', 'تأكيد', 'تاكيد', 'موافق'}:
        return confirm_admin_action(sender_id)
    if low in {'لا', 'الغاء', 'إلغاء', 'cancel'}:
        action = _admin_pop_pending(sender_id)
        msg = 'تم إلغاء الأمر المعلّق.' if action else 'ما في أمر معلّق لإلغائه.'
        _admin_audit(sender_id, 'cancel_pending', raw, msg)
        return {'ok': bool(action), 'message': msg, 'code': 'CANCELLED' if action else 'NO_PENDING'}

    try:
        if any(k in low for k in ['مساعده المدير', 'اوامر المدير', 'اوامر الاداره', 'شو فيني اعدل', 'ماذا تستطيع ان تنفذ']):
            msg = (
                'أوامر الإدارة المتاحة حالياً:\n'
                '• أضف دورة ...\n'
                '• عدّل دورة ...\n'
                '• عطّل/فعّل دورة ...\n'
                '• احذف دورة ... (يحتاج تأكيد)\n'
                '• أضف موعد بدء لدورة ...\n'
                '• عدّل موعد بدء لدورة ...\n'
                '• أضف معلومة المفتاح = القيمة\n'
                '• عدّل معلومة المفتاح = القيمة\n'
                '• غيّر رقم الواتساب إلى ... أو غيّر العنوان إلى ...\n'
                '• احذف معلومة المفتاح (يحتاج تأكيد)\n'
                '• اعرض الدورات\n'
                '• اعرض المواعيد\n'
                '• اعرض المعلومات\n'
                '• احصائيات البوت\n'
                '• أوقف الرد الآلي عن مستخدم <ID>\n'
                '• فعّل الرد الآلي عن مستخدم <ID>'
            )
            _admin_audit(sender_id, 'help', raw, msg)
            return {'ok': True, 'message': msg}

        if 'احصائيات' in low or 'احصائيات البوت' in low:
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    total_messages = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
                    users = conn.execute('SELECT COUNT(DISTINCT sender_id) FROM conversations').fetchone()[0]
                    paused = conn.execute('SELECT COUNT(*) FROM human_handover WHERE is_paused=1').fetchone()[0]
                    events = conn.execute("SELECT COUNT(*) FROM webhook_events WHERE status IN ('queued','pending','processing')").fetchone()[0]
                finally:
                    conn.close()
            msg = f'إحصائيات البوت:\nالرسائل: {total_messages}\nالمستخدمين: {users}\nالمحادثات المحولة لموظف: {paused}\nالأحداث قيد المعالجة: {events}'
            _admin_audit(sender_id, 'stats', raw, msg)
            return {'ok': True, 'message': msg}

        if 'اعرض الدورات' in low or 'قائمة الدورات' in low or 'شو الدورات' in low:
            msg = _format_courses(admin_list_courses())
            _admin_audit(sender_id, 'list_courses', raw, msg)
            return {'ok': True, 'message': msg}

        if 'اعرض المواعيد' in low or 'قائمة المواعيد' in low:
            msg = _format_batches(admin_list_batches())
            _admin_audit(sender_id, 'list_batches', raw, msg)
            return {'ok': True, 'message': msg}

        if 'اعرض المعلومات' in low or 'المعلومات الإضافية' in low or 'المعلومات الاضافية' in low:
            data = _all_dynamic_knowledge()
            msg = 'المعلومات الإضافية:\n' + ('\n'.join(f'- {k}: {v}' for k, v in data) if data else 'لا توجد معلومات إضافية.')
            _admin_audit(sender_id, 'list_info', raw, msg)
            return {'ok': True, 'message': msg}

        # Generic key=value knowledge store.
        info_prefix = None
        for prefix in ['اضف معلومة', 'أضف معلومة', 'عدل معلومة', 'عدّل معلومة', 'تعديل معلومة', 'اضافة معلومة']:
            if low.startswith(_normalize_admin_text(prefix)):
                info_prefix = prefix
                break
        if info_prefix:
            rest = raw[len(info_prefix):].strip(' :،,')
            if '=' not in rest:
                return {'ok': False, 'message': 'اكتبها بهذا الشكل: أضف معلومة اسم المعلومة = القيمة', 'code': 'BAD_FORMAT'}
            key, value = [x.strip() for x in rest.split('=', 1)]
            if not key or not value:
                return {'ok': False, 'message': 'اسم المعلومة والقيمة مطلوبان.', 'code': 'BAD_FORMAT'}
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    conn.execute(
                        'INSERT INTO academy_info(info_key, info_value) VALUES (?,?) ON CONFLICT(info_key) DO UPDATE SET info_value=excluded.info_value, updated_at=CURRENT_TIMESTAMP',
                        (key, value),
                    )
                    conn.commit()
                finally:
                    conn.close()
            msg = f'تم حفظ المعلومة: {key} = {value}'
            _admin_audit(sender_id, 'set_info', raw, msg)
            return {'ok': True, 'message': msg}

        # Natural-language generic academy setting updates, e.g.:
        # "غيّر رقم الواتساب إلى 099..." or "عدل عنوان المركز الى ..."
        if low.startswith('غير ') or low.startswith('غيّر ') or low.startswith('عدل ') or low.startswith('عدّل '):
            import re
            m = re.match(r'(?:غير|غيّر|عدل|عدّل)\s+(.+?)\s+(?:الى|إلى)\s+(.+)$', raw, re.IGNORECASE)
            if m and 'دورة' not in m.group(1) and 'موعد' not in m.group(1):
                key = m.group(1).strip(' :،,')
                value = m.group(2).strip()
                if key and value:
                    with DB_LOCK:
                        conn = get_db_connection()
                        try:
                            conn.execute(
                                'INSERT INTO academy_info(info_key, info_value) VALUES (?,?) ON CONFLICT(info_key) DO UPDATE SET info_value=excluded.info_value, updated_at=CURRENT_TIMESTAMP',
                                (key, value),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    msg = f'تم تعديل المعلومة «{key}» إلى: {value}'
                    _admin_audit(sender_id, 'set_info_natural', raw, msg)
                    return {'ok': True, 'message': msg}

        if low.startswith('احذف معلومة') or low.startswith('احذف معلومات'):
            key = raw.split('معلومة', 1)[1].strip(' :،,') if 'معلومة' in raw else raw.split('معلومات',1)[1].strip()
            if not key:
                return {'ok': False, 'message': 'حدد اسم المعلومة التي تريد حذفها.', 'code': 'BAD_FORMAT'}
            _admin_set_pending(sender_id, {'action': 'delete_info', 'key': key})
            msg = f'هذا سيحذف المعلومة «{key}» نهائياً. اكتب «نعم» للتأكيد خلال دقيقتين.'
            _admin_audit(sender_id, 'confirm_delete_info', raw, msg)
            return {'ok': False, 'message': msg, 'code': 'CONFIRM_REQUIRED'}

        if low.startswith('احذف دورة') or low.startswith('حذف دورة'):
            name = _extract_course_name(raw)
            if not name:
                return {'ok': False, 'message': 'حدد اسم الدورة التي تريد حذفها.', 'code': 'BAD_FORMAT'}
            _admin_set_pending(sender_id, {'action': 'delete_course', 'name': name})
            msg = f'هذا سيحذف دورة «{name}» وجداول بداياتها. اكتب «نعم» للتأكيد خلال دقيقتين.'
            _admin_audit(sender_id, 'confirm_delete_course', raw, msg)
            return {'ok': False, 'message': msg, 'code': 'CONFIRM_REQUIRED'}

        if 'أوقف الرد الآلي' in raw or 'اوقف الرد الالي' in low:
            import re
            m = re.search(r'([0-9]{5,})', raw)
            if not m:
                return {'ok': False, 'message': 'أرسل معرف المستخدم بعد الأمر.', 'code': 'BAD_FORMAT'}
            user_id = m.group(1)
            set_handover_status(user_id, 1)
            msg = f'تم إيقاف الرد الآلي عن المستخدم {user_id}.'
            _admin_audit(sender_id, 'pause_user', raw, msg)
            return {'ok': True, 'message': msg}

        if 'فعّل الرد الآلي' in raw or 'فعل الرد الالي' in low:
            import re
            m = re.search(r'([0-9]{5,})', raw)
            if not m:
                return {'ok': False, 'message': 'أرسل معرف المستخدم بعد الأمر.', 'code': 'BAD_FORMAT'}
            user_id = m.group(1)
            set_handover_status(user_id, 0)
            msg = f'تم تفعيل الرد الآلي عن المستخدم {user_id}.'
            _admin_audit(sender_id, 'resume_user', raw, msg)
            return {'ok': True, 'message': msg}

        # Course creation/update.
        if low.startswith('اضف دورة') or low.startswith('أضف دورة'):
            name = _extract_course_name(raw)
            if not name:
                return {'ok': False, 'message': 'حدد اسم الدورة. مثال: أضف دورة ميك أب متقدم، 16 درس، السعر 900000', 'code': 'BAD_FORMAT'}
            lessons = _extract_int_field(text, ['عدد الدروس','الدروس','درس'])
            price = _extract_int_field(text, ['السعر','سعر'])
            deposit = _extract_int_field(text, ['الدفعة الأولى','الدفعة الاولي','دفعة اولى','الدفعة'])
            start_date = _extract_date(text)
            days = _extract_int_field(text, ['أيام بالأسبوع','ايام بالاسبوع','3 ايام بالاسبوع','أيام'])
            duration = _extract_field(text, ['المدة','مدة الدرس'])
            topics = _extract_field(text, ['المحاور','محاور'])
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    conn.execute(
                        'INSERT INTO academy_courses(name,lessons,duration_text,days_per_week,price,first_payment,start_date,topics) VALUES (?,?,?,?,?,?,?,?)',
                        (name, lessons, duration, days, price, deposit, start_date, topics),
                    )
                    conn.commit()
                finally:
                    conn.close()
            msg = f'تمت إضافة دورة «{name}» بنجاح.'
            _admin_audit(sender_id, 'add_course', raw, msg)
            return {'ok': True, 'message': msg}

        if low.startswith('عدل دورة') or low.startswith('عدّل دورة') or low.startswith('تعديل دورة'):
            name = _extract_course_name(raw)
            if not name:
                return {'ok': False, 'message': 'حدد اسم الدورة التي تريد تعديلها.', 'code': 'BAD_FORMAT'}
            fields = {
                'lessons': _extract_int_field(text, ['عدد الدروس','الدروس']),
                'price': _extract_int_field(text, ['السعر','سعر']),
                'first_payment': _extract_int_field(text, ['الدفعة الأولى','الدفعة الاولي','دفعة اولى','الدفعة']),
                'start_date': _extract_date(text),
                'days_per_week': _extract_int_field(text, ['أيام بالأسبوع','ايام بالاسبوع','أيام']),
                'duration_text': _extract_field(text, ['المدة','مدة الدرس']),
                'topics': _extract_field(text, ['المحاور','محاور']),
            }
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    ok, msg = _set_course(conn, name, fields)
                    if ok: conn.commit()
                finally:
                    conn.close()
            _admin_audit(sender_id, 'update_course', raw, msg)
            return {'ok': ok, 'message': msg}

        if 'عطل دورة' in low or 'عطّل دورة' in raw:
            name = _extract_course_name(raw)
            if not name:
                return {'ok': False, 'message': 'حدد اسم الدورة.', 'code': 'BAD_FORMAT'}
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    exists = _find_course(conn, name)
                    if not exists:
                        msg = f'ما لقيت دورة باسم: {name}'
                        ok = False
                    else:
                        conn.execute('UPDATE academy_courses SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', (exists[0],))
                        conn.commit(); ok = True; msg = f'تم تعطيل دورة {name}.'
                finally: conn.close()
            _admin_audit(sender_id, 'disable_course', raw, msg)
            return {'ok': ok, 'message': msg}

        if 'فعل دورة' in low or 'فعّل دورة' in raw:
            name = _extract_course_name(raw)
            if not name:
                return {'ok': False, 'message': 'حدد اسم الدورة.', 'code': 'BAD_FORMAT'}
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    exists = _find_course(conn, name)
                    if not exists:
                        msg = f'ما لقيت دورة باسم: {name}'
                        ok = False
                    else:
                        conn.execute('UPDATE academy_courses SET active=1, updated_at=CURRENT_TIMESTAMP WHERE id=?', (exists[0],))
                        conn.commit(); ok = True; msg = f'تم تفعيل دورة {name}.'
                finally: conn.close()
            _admin_audit(sender_id, 'enable_course', raw, msg)
            return {'ok': ok, 'message': msg}

        # Batch start date management.
        if 'أضف موعد بدء' in raw or 'اضف موعد بدء' in low:
            import re
            m = re.search(r'(?:لدورة|للدورة)\s+(.+?)(?=\s*[:،,;]\s*20\d{2}|$)', raw)
            name = m.group(1).strip(' :،,;') if m else _extract_course_name(raw)
            start_date = _extract_date(raw)
            schedule = _extract_field(text, ['الدوام','جدول','وقت'])
            if not name or not start_date:
                return {'ok': False, 'message': 'اكتب: أضف موعد بدء لدورة اسم الدورة: 2026-09-15', 'code': 'BAD_FORMAT'}
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    course = _find_course(conn, name)
                    if not course:
                        return {'ok': False, 'message': f'ما لقيت دورة باسم: {name}', 'code': 'NOT_FOUND'}
                    conn.execute('INSERT INTO course_batches(course_id,start_date,schedule_text) VALUES (?,?,?)', (course[0], start_date, schedule))
                    conn.commit()
                finally: conn.close()
            msg = f'تمت إضافة موعد بدء دورة {name}: {start_date}.'
            _admin_audit(sender_id, 'add_batch', raw, msg)
            return {'ok': True, 'message': msg}

        if 'عدل موعد بدء' in low or 'عدّل موعد بدء' in raw or 'تعديل موعد بدء' in low:
            import re
            m = re.search(r'(?:لدورة|للدورة|دورة)\s+(.+?)(?=\s*(?:إلى|الى|:|،|,)\s*20\d{2}|$)', raw)
            name = m.group(1).strip(' :،,;') if m else _extract_course_name(raw)
            start_date = _extract_date(raw)
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    course = _find_course(conn, name)
                    if not course:
                        return {'ok': False, 'message': f'ما لقيت دورة باسم: {name}', 'code': 'NOT_FOUND'}
                    conn.execute('UPDATE course_batches SET start_date=? WHERE course_id=? AND active=1', (start_date, course[0]))
                    conn.execute('UPDATE academy_courses SET start_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (start_date, course[0]))
                    conn.commit()
                finally: conn.close()
            msg = f'تم تحديث موعد بدء {name} إلى {start_date}.'
            _admin_audit(sender_id, 'update_batch', raw, msg)
            return {'ok': True, 'message': msg}

        return {'ok': False, 'message': None, 'code': 'NOT_ADMIN_COMMAND'}
    except sqlite3.IntegrityError as exc:
        msg = f'تعذر تنفيذ الأمر بسبب تعارض في البيانات: {exc}'
        _admin_audit(sender_id, 'error', raw, msg)
        return {'ok': False, 'message': msg, 'code': 'DB_CONFLICT'}
    except Exception as exc:
        msg = f'حدث خطأ أثناء تنفيذ الأمر: {exc}'
        _admin_audit(sender_id, 'error', raw, msg)
        return {'ok': False, 'message': msg, 'code': 'ERROR'}


def confirm_admin_action(sender_id):
    action = _admin_pop_pending(sender_id)
    if not action:
        return {'ok': False, 'message': 'ما في أمر إداري معلّق للتأكيد أو انتهت مهلة التأكيد.', 'code': 'NO_PENDING'}
    try:
        if action['action'] == 'delete_info':
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    conn.execute('DELETE FROM academy_info WHERE info_key=?', (action['key'],))
                    conn.commit()
                finally: conn.close()
            msg = f'تم حذف المعلومة «{action["key"]}».'
            _admin_audit(sender_id, 'delete_info', 'تأكيد', msg)
            return {'ok': True, 'message': msg}
        if action['action'] == 'delete_course':
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    row = _find_course(conn, action['name'])
                    if not row:
                        msg = f'ما لقيت دورة باسم: {action["name"]}'
                        return {'ok': False, 'message': msg, 'code': 'NOT_FOUND'}
                    conn.execute('DELETE FROM course_batches WHERE course_id=?', (row[0],))
                    conn.execute('DELETE FROM academy_courses WHERE id=?', (row[0],))
                    conn.commit()
                finally: conn.close()
            msg = f"تم حذف دورة «{action['name']}»."
            _admin_audit(sender_id, 'delete_course', 'تأكيد', msg)
            return {'ok': True, 'message': msg}
        return {'ok': False, 'message': 'الأمر المعلّق غير معروف.', 'code': 'UNKNOWN_PENDING'}
    except Exception as exc:
        return {'ok': False, 'message': f'فشل التأكيد: {exc}', 'code': 'ERROR'}


def clear_admin_store_for_tests():
    """Test helper: clears only manager-managed data, never user conversations."""
    with DB_LOCK:
        conn = get_db_connection()
        try:
            for table in ['course_batches','academy_courses','academy_info','admin_audit_log','admin_pending_actions']:
                conn.execute(f'DELETE FROM {table}')
            conn.commit()
        finally:
            conn.close()


def build_dynamic_academy_knowledge():
    _seed_legacy_courses_if_empty()
    courses = admin_list_courses()
    batches = admin_list_batches()
    info = _all_dynamic_knowledge()
    lines = ['\n\n=== البيانات الحالية التي يديرها المدير ===']
    for c in courses:
        lines.append(
            f"- {c['name']}: {c['lessons'] or '?'} درس؛ مدة الدرس {c['duration_text'] or 'غير محددة'}؛ {c['days_per_week'] or '?'} أيام بالأسبوع؛ السعر {c['price'] if c['price'] is not None else 'غير محدد'}؛ الدفعة الأولى {c['first_payment'] if c['first_payment'] is not None else 'غير محددة'}؛ البداية {c['start_date'] or 'غير محددة'}؛ المحاور {c['topics'] or 'غير محددة'}"
        )
    if batches:
        lines.append('مواعيد إضافية:')
        for b in batches:
            lines.append(f"- {b['course_name']}: {b['start_date']} {b['schedule_text'] or ''}".strip())
    if info:
        lines.append('معلومات إضافية:')
        for k, v in info:
            lines.append(f'- {k}: {v}')
    return '\n'.join(lines)


# ========================================================
# 🧠 Prompt modularization
# ========================================================
SYSTEM_POLICY = """
أنت المساعد الذكي الرسمي لـ "الأكاديمية الدولية للتدريب المهني" في حمص.
شخصيتك: طبيعي، مريح، مؤدب، لهجة سورية عامية راقية، نبرة مبيعات استشارية.
⛔ ممنوع: الفواصل الجندرية (حابب/حابة)، العبارات الخشبية (دواعي سرورنا)، ومصطلحات الابتذال (لعيونك، يا غالي).
⛔ المخاطبة: صيغة المؤنث هي الأساس لجميع استفسارات التجميل والمكياج والبشرة والأظافر والشنيون.
⛔ صيغة المذكر فقط إذا كان الاستفسار عن الحلاقة الرجالية أو صرّح الشخص أنه شاب.
⛔ إذا احتوى السياق على [مدير الأكاديمية]، تعامل معه باحترام كامل للمدير.
"""

SALES_RULES = """
⛔ قواعد الإقناع وتدفق المحادثة:
1. الإيجاز: لا ترسل السعر والمحاور والشهادات معاً. افصلها بأسئلة.
2. بناء القيمة: قبل السعر، اشرح القيمة (تدريب عملي، مواد مؤمنة، شهادة).
3. توضيح الأقساط: السعر يقسم لدفعة أولى للتثبيت والباقي دفعات مرنة.
4. تثبيت عن بعد: اقترح الدفع عبر شام كاش للحجز عن بعد.
5. ضمان الإتقان: أكد على ضمان الإتقان وإمكانية إعادة الدروس مجاناً مع القاعة التالية.
6. المحاور: في نهاية كل محور يجب إضافة الملاحظة: "(ملاحظة: هذه رؤوس أقلام والمحور الشامل تفصيلي جداً، ولكن يتعذر إرساله بالكامل لأن الرسالة ستكون طويلة جداً)".
"""

ACADEMY_KNOWLEDGE = """
⛔ توضيح الأدوات: أدوات ومواد التدريب موفرة بالكامل داخل الأكاديمية لاستخدامكِ أثناء الدروس.
مواعيد الاستقبال: يومياً من 10:30 صباحاً وحتى 5:00 مساءً.
العنوان: حمص - شارع الحضارة - دخلة وكالة مابكو - جانب مكياجات الحضارة - مقابل نظارات غنوم.
الهاتف والواتساب: 0932775583.
التثبيت: حضور شخصي (صورة هوية + الدفعة الأولى) أو عن بعد عبر شام كاش.
تفاصيل الشهادات: الشهادة والتصديقات الرسمية مشمولة بسعر الدورة ولا توجد تكاليف إضافية.

مهم جداً: بيانات الدورات والمواعيد والمعلومات الإضافية التي تُضاف إلى النظام يتم إدراجها بعد هذه التعليمات في قسم "البيانات الحالية التي يديرها المدير"، وهي المصدر الأحدث والواجب اعتماده عند التعارض مع أي بيانات ثابتة.
"""

SYSTEM_INSTRUCTION = f"{SYSTEM_POLICY}\n\n{SALES_RULES}\n\n{ACADEMY_KNOWLEDGE}"


# ========================================================
# 🔒 Security helpers
# ========================================================
def verify_signature_bytes(raw_body: bytes, signature: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def verify_facebook_signature(flask_request) -> bool:
    return verify_signature_bytes(
        flask_request.get_data(cache=True),
        flask_request.headers.get("X-Hub-Signature-256", ""),
    )


def verify_admin_password(candidate: str) -> bool:
    return hmac.compare_digest(
        candidate.strip().encode("utf-8"),
        ADMIN_PASSWORD.encode("utf-8"),
    )


def check_admin_lockout(sender_id, now=None):
    now = time.time() if now is None else now
    with ADMIN_ATTEMPTS_LOCK:
        attempts, lock_time = ADMIN_ATTEMPTS.get(sender_id, (0, 0))
        if now < lock_time:
            return True, lock_time - now
        if lock_time and now >= lock_time:
            ADMIN_ATTEMPTS.pop(sender_id, None)
        return False, 0


def record_admin_attempt(sender_id, success, now=None):
    now = time.time() if now is None else now
    with ADMIN_ATTEMPTS_LOCK:
        if success:
            ADMIN_ATTEMPTS.pop(sender_id, None)
            return
        attempts, lock_time = ADMIN_ATTEMPTS.get(sender_id, (0, 0))
        attempts += 1
        if attempts >= ADMIN_MAX_ATTEMPTS:
            lock_time = now + ADMIN_LOCKOUT_SECONDS
            attempts = ADMIN_MAX_ATTEMPTS
        ADMIN_ATTEMPTS[sender_id] = (attempts, lock_time)


def prune_dedup_cache(now=None):
    now = time.time() if now is None else now
    with PROCESSED_MESSAGES_LOCK:
        expired = [
            key
            for key, timestamp in PROCESSED_MESSAGES.items()
            if now - timestamp > DEDUP_TTL_SECONDS
        ]
        for key in expired:
            del PROCESSED_MESSAGES[key]


def is_duplicate_message(message_id):
    if not message_id:
        return False
    now = time.time()
    with PROCESSED_MESSAGES_LOCK:
        expired = [
            key
            for key, timestamp in PROCESSED_MESSAGES.items()
            if now - timestamp > DEDUP_TTL_SECONDS
        ]
        for key in expired:
            del PROCESSED_MESSAGES[key]
        if message_id in PROCESSED_MESSAGES:
            return True
        PROCESSED_MESSAGES[message_id] = now
        return False


def wait_for_user_rate_limit(sender_id):
    with LAST_PROCESSED_LOCK:
        now = time.time()
        last = LAST_PROCESSED_TIME.get(sender_id, 0.0)
        wait = max(0.0, USER_RATE_LIMIT_SECONDS - (now - last))
        if wait:
            # Marking happens after the wait, outside the lock, to avoid blocking others.
            pass
    if wait:
        time.sleep(wait)
    with LAST_PROCESSED_LOCK:
        LAST_PROCESSED_TIME[sender_id] = time.time()


def get_user_lock(sender_id):
    return USER_LOCKS[hash(sender_id) % USER_LOCK_COUNT]


# ========================================================
# 🌐 Facebook HTTP reliability
# ========================================================
def facebook_post(payload: dict, retry_attempts=FACEBOOK_RETRY_ATTEMPTS):
    url = "https://graph.facebook.com/v19.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    last_error = None

    for attempt in range(retry_attempts):
        try:
            response = FACEBOOK_SESSION.post(
                url,
                params=params,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=FACEBOOK_TIMEOUT_SECONDS,
            )
            if 200 <= response.status_code < 300:
                return response

            last_error = RuntimeError(
                f"Facebook API {response.status_code}: {response.text[:500]}"
            )
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                raise last_error
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if isinstance(exc, RuntimeError) and "Facebook API 4" in str(exc) and "429" not in str(exc):
                raise

        if attempt < retry_attempts - 1:
            time.sleep(min(2 ** attempt, 4))

    raise last_error or RuntimeError("Facebook API request failed")


def send_typing_indicator(recipient_id):
    try:
        facebook_post(
            {
                "recipient": {"id": recipient_id},
                "sender_action": "typing_on",
            },
            retry_attempts=2,
        )
    except Exception as exc:
        print(f"Failed to send typing indicator: {exc}")


def send_facebook_message(recipient_id, message_text, quick_replies=None):
    message = {"text": message_text}
    if quick_replies:
        message["quick_replies"] = quick_replies
    try:
        facebook_post({"recipient": {"id": recipient_id}, "message": message})
        return True
    except Exception as exc:
        print(f"Failed to send FB message: {exc}")
        return False


# ========================================================
# 🤖 AI and business logic
# ========================================================
def generate_ai_reply(sender_id, user_message, intent=None, is_admin=False, message_id=None):
    try:
        save_message_db(
            sender_id,
            "user",
            user_message,
            intent=intent,
            message_id=message_id,
        )
        conversation_history = get_user_history_db(sender_id, limit=12)

        current_text = user_message
        if intent:
            current_text = (
                f"[نية محددة من النظام بناءً على زر: {intent}]\n"
                f"{user_message}"
            )
        if is_admin:
            current_text = (
                "[مدير الأكاديمية]\n" + current_text
            )

        # Keep the current message as the last user turn while retaining the existing history.
        contents = conversation_history[:-1] if conversation_history else []
        contents.append({"role": "user", "parts": [{"text": current_text}]})

        system_instruction = SYSTEM_INSTRUCTION + build_dynamic_academy_knowledge()
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config={"system_instruction": system_instruction},
        )

        reply_text = (response.text or "").strip()
        if not reply_text:
            raise RuntimeError("Gemini returned an empty response")

        save_message_db(sender_id, "model", reply_text)
        return reply_text
    except Exception as exc:
        print(f"Error in Gemini API: {exc}")
        return "أهلاً بك! يمكنك التواصل مع إدارة الأكاديمية مباشرة على الرقم: 0932775583"


def determine_quick_replies(reply_text):
    if "محتارة" in reply_text or "اهتمامك" in reply_text:
        return [
            {"content_type": "text", "title": "شغل فني (أظافر ومكياج)", "payload": "ARTISTIC"},
            {"content_type": "text", "title": "عناية بالبشرة", "payload": "SKINCARE"},
            {"content_type": "text", "title": "الشعر والتسريحات", "payload": "HAIR"},
        ]
    if "شو حابة تعرفي" in reply_text or "تسهيلات الدفع" in reply_text:
        return [
            {"content_type": "text", "title": "السعر والأقساط", "payload": "PRICE"},
            {"content_type": "text", "title": "المحاور والدروس", "payload": "SYLLABUS"},
            {"content_type": "text", "title": "تثبيت (شام كاش)", "payload": "SHAM_CASH"},
        ]
    if "المحاور والدروس" in reply_text or "رؤوس أقلام" in reply_text:
        return [
            {"content_type": "text", "title": "المحاور والدروس", "payload": "SYLLABUS"},
            {"content_type": "text", "title": "تفاصيل الشهادة", "payload": "CERTIFICATE"},
            {"content_type": "text", "title": "عنوان المركز والمواعيد", "payload": "LOCATION"},
        ]
    if "عنوان" in reply_text or "الشهادات" in reply_text or "إعادة" in reply_text:
        return [
            {"content_type": "text", "title": "تثبيت (شام كاش)", "payload": "SHAM_CASH"},
            {"content_type": "text", "title": "عنوان المركز والمواعيد", "payload": "LOCATION"},
            {"content_type": "text", "title": "تواصل مع الإدارة", "payload": "HUMAN_HANDOVER"},
        ]
    return None


# ========================================================
# ⚙️ Event processing
# ========================================================
def process_single_message(event_payload, event_id=None):
    if not event_payload.get("message"):
        return
    message = event_payload["message"]
    if message.get("is_echo"):
        return

    sender_id = event_payload["sender"]["id"]
    message_id = message.get("mid")
    user_text = (message.get("text") or "").strip()
    quick_reply = message.get("quick_reply") or {}
    intent = quick_reply.get("payload")

    if not user_text and not intent:
        return

    # Deduplication is performed at Webhook ingestion. Retries from the durable inbox
    # must be allowed to reach Facebook again after transient delivery failures.

    # Per-user serialization keeps conversational history ordered.
    with get_user_lock(sender_id):
        wait_for_user_rate_limit(sender_id)
        send_typing_indicator(sender_id)
        admin_info = get_admin_status(sender_id)

        # Admin password flow must be completed after explicit admin claim.
        if admin_info["awaiting_password"]:
            is_locked, time_left = check_admin_lockout(sender_id)
            if is_locked:
                if not send_facebook_message(
                    sender_id,
                    f"تم قفل محاولات الإدارة. يرجى الانتظار {max(1, int(time_left // 60))} دقيقة.",
                ):
                    raise RuntimeError("Failed to deliver admin lockout message")
                return

            if verify_admin_password(user_text):
                record_admin_attempt(sender_id, True)
                set_admin_status(sender_id, is_admin=1, awaiting_password=0)
                set_handover_status(sender_id, status=0)
                if not send_facebook_message(
                    sender_id,
                    "تم التحقق بنجاح! أهلاً بك مديرنا العزيز. كيف يمكنني مساعدتك؟",
                ):
                    raise RuntimeError("Failed to deliver admin success message")
            else:
                record_admin_attempt(sender_id, False)
                if not send_facebook_message(
                    sender_id,
                    "كلمة السر غير صحيحة. يرجى إعادة المحاولة:",
                ):
                    raise RuntimeError("Failed to deliver admin failure message")
            return

        # Explicit admin claim -> ask for password, do not grant privileges.
        if any(
            claim in user_text
            for claim in ["أنا المدير", "انا المدير", "صاحب المركز", "إدارة المركز", "ادارة المركز"]
        ) and not admin_info["is_admin"]:
            set_admin_status(sender_id, is_admin=0, awaiting_password=1)
            if not send_facebook_message(sender_id, "يرجى إدخال كلمة السر الخاصة بالإدارة لتأكيد الهوية:"):
                raise RuntimeError("Failed to deliver admin prompt")
            return

        # Verified manager commands are deterministic and audited. Gemini is only used
        # for natural-language understanding when no concrete admin tool matches.
        if admin_info["is_admin"]:
            admin_result = admin_execute(sender_id, user_text)
            if admin_result.get("code") != "NOT_ADMIN_COMMAND":
                admin_message = admin_result.get("message") or "تمت معالجة الأمر الإداري."
                if not send_facebook_message(sender_id, admin_message):
                    raise RuntimeError("Failed to deliver admin command response")
                return

        # Human handover controls are deterministic and do not consume Gemini.
        handover_keywords = ["موظف", "بشري", "تواصل مباشر", "احكي مع حدا", "أحكي مع حدا", "تحدث مع انسان"]
        unpause_keywords = ["تشغيل البوت", "تفعيل البوت", "إعادة البوت", "اعادة البوت"]

        if any(kw in user_text for kw in unpause_keywords):
            set_handover_status(sender_id, status=0)
            if not send_facebook_message(sender_id, "تم تفعيل الرد التلقائي للبوت بنجاح! تفضلي كيف بقدر أساعدك؟"):
                raise RuntimeError("Failed to deliver unpause message")
            return

        if is_user_paused(sender_id) and not admin_info["is_admin"]:
            return

        if any(kw in user_text for kw in handover_keywords) and not admin_info["is_admin"]:
            set_handover_status(sender_id, status=1)
            if not send_facebook_message(
                sender_id,
                "تم تحويل طلبك لموظف المتابعة الإدارية وسيقوم الفريق بالرد عليكِ قريباً. (لتفعيل البوت مجدداً أرسلي: تشغيل البوت)",
            ):
                raise RuntimeError("Failed to deliver handover message")
            return

        ai_response = generate_ai_reply(
            sender_id,
            user_text,
            intent=intent,
            is_admin=admin_info["is_admin"],
            message_id=message_id,
        )
        quick_replies = determine_quick_replies(ai_response)
        if event_id is not None:
            save_event_response(event_id, ai_response, quick_replies)
        if not send_facebook_message(sender_id, ai_response, quick_replies):
            raise RuntimeError("Failed to deliver Gemini response to Facebook")


# ========================================================
# 🔄 Worker pool: bounded RAM, DB-backed pending events
# ========================================================
def process_event_record(event_id: int):
    print(f"[EVENT] loading event_id={event_id}", flush=True)
    event = load_event(event_id)
    if not event:
        mark_event_completed(event_id)
        return

    try:
        if event.get("response_text"):
            if not send_facebook_message(
                event["sender_id"],
                event["response_text"],
                event.get("response_quick_replies"),
            ):
                raise RuntimeError("Failed to redeliver stored response to Facebook")
        else:
            print(f"[EVENT] processing event_id={event_id}", flush=True)
            process_single_message(event["payload"], event_id=event_id)
        mark_event_completed(event_id)
        print(f"[EVENT] completed event_id={event_id}", flush=True)
    except Exception as exc:
        print(f"Event {event_id} failed: {exc}")
        mark_event_failed(event_id, str(exc), retryable=True)


def worker_iteration(timeout=0.5):
    """Process at most one event. RAM-queued events are claimed once; DB-recovered events are already claimed."""
    event_id = None
    from_queue = False
    try:
        try:
            event_id = MESSAGE_QUEUE.get(timeout=timeout)
            from_queue = True
        except queue.Empty:
            # This function atomically changes pending/queued -> processing.
            event_id = claim_next_pending_event()

        if event_id is None:
            return None

        if from_queue:
            # A freshly enqueued event is still queued in SQLite. This is the one and only
            # claim for the RAM-queue path. Do not claim a second time.
            if not claim_queued_or_pending_event(event_id):
                print(f"[WORKER] event_id={event_id} was already claimed; skipping", flush=True)
                return None
        # For the DB-recovery path, claim_next_pending_event() already set processing.

        print(f"[WORKER] claimed event_id={event_id} source={'ram' if from_queue else 'db'}", flush=True)
        process_event_record(event_id)
        return event_id
    except Exception as exc:
        print(f"[WORKER] iteration error: {type(exc).__name__}: {exc}", flush=True)
        if event_id is not None:
            try:
                mark_event_failed(event_id, str(exc), retryable=True)
            except Exception as mark_exc:
                print(f"[WORKER] failed to mark event_id={event_id}: {type(mark_exc).__name__}: {mark_exc}", flush=True)
        return None
    finally:
        if from_queue:
            try:
                MESSAGE_QUEUE.task_done()
            except ValueError:
                pass


def worker_loop(worker_number: int):
    print(f"[WORKER-{worker_number}] started", flush=True)
    while not STOP_EVENT.is_set():
        worker_iteration(timeout=0.5)
    print(f"[WORKER-{worker_number}] stopped", flush=True)


def start_workers():
    reset_stale_processing_events()
    if WORKERS:
        return
    print(f"[STARTUP] starting {WORKER_COUNT} worker(s), queue_max={MESSAGE_QUEUE_MAXSIZE}", flush=True)
    for i in range(WORKER_COUNT):
        thread = threading.Thread(
            target=worker_loop,
            args=(i + 1,),
            name=f"academy-bot-worker-{i + 1}",
            daemon=True,
        )
        thread.start()
        WORKERS.append(thread)


# ========================================================
# 🌐 Meta Webhook
# ========================================================
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "workers": WORKER_COUNT, "queue_size": MESSAGE_QUEUE.qsize()}, 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return challenge, 200
    return "Verify token mismatch", 403


@app.route("/webhook", methods=["POST"])
def handle_messages():
    # Signature verification is mandatory in production.
    if not verify_facebook_signature(request):
        return "Invalid Signature", 403

    data = request.get_json(silent=True) or {}
    if data.get("object") != "page":
        return "EVENT_RECEIVED", 200

    for entry in data.get("entry", []):
        for messaging_event in entry.get("messaging", []):
            if messaging_event.get("message") and not messaging_event["message"].get("is_echo"):
                sender_id = messaging_event.get("sender", {}).get("id")
                if not sender_id:
                    continue
                message_id = messaging_event.get("message", {}).get("mid")
                if message_id and is_duplicate_message(message_id):
                    print(f"[WEBHOOK] duplicate mid={message_id}", flush=True)
                    continue
                print(f"[WEBHOOK] received sender={sender_id} mid={message_id}", flush=True)
                enqueue_webhook_event(sender_id, message_id, messaging_event)

    return "EVENT_RECEIVED", 200


# ========================================================
# 🚀 Startup
# ========================================================
init_db()
_seed_legacy_courses_if_empty()
if AUTO_START_WORKERS:
    start_workers()
else:
    print("[STARTUP] workers disabled by BOT_AUTO_START_WORKERS=0", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
