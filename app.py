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
        try:
            MESSAGE_QUEUE.put_nowait(event_id)
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
    return event_id


def claim_next_pending_event() -> Optional[int]:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id FROM webhook_events
                WHERE status='pending' AND available_at <= ?
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
                WHERE id=? AND status='pending'
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
                "SELECT id, sender_id, payload, retries FROM webhook_events WHERE id=?",
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

📊 تفاصيل الدورات:
- تنظيف وعناية بالبشرة: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 1,500,000 ل.س (دفعة أولى 400,000 ل.س). المحاور: تشخيص البشرة، أجهزة الهيدرافشيال والديرمابن والتقشير الكريستالي، المستحضرات، المساج والتعقيم.
- حلاقة نسائية: 20 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 950,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: قصات حديثة، سيشوار وفير، صبغ وتخصيل والعناية بالشعر.
- شنيون وتسريحات: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 850,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: تسريحات عرائس 3D، تسريحات مرفوعة ومنسدلة، تثبيت الإكسسوارات والطرحة.
- جل أظافر Gel Nails: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 750,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: تقنيات عادية وروسية، تمديد وتكثيف (اكستنشن وفايبر)، رتوش، فرنش ورسم، تطبيق على موديل.
- حلاقة رجالية: 20 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 750,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: قصات حديثة، حلاقة وتحديد اللحية، استخدام الموس، سيشوار، صبغة وحناء.
- إكستنيشن رموش & Lash Lifting: 12 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 700,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: تركيب وعزل الرموش، رفع وتثبيت الرموش، والعناية بعد الجلسة.
- ميك أب احترافي Make-up: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر: 700,000 ل.س (دفعة أولى 200,000 ل.س). المحاور: تهيئة البشرة، فونديشن وكوركتر، آيلاينر وإيشادو، كونتور، تركيب رموش، لوكات ناعمة وسهرات.
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

        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config={"system_instruction": SYSTEM_INSTRUCTION},
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
            process_single_message(event["payload"], event_id=event_id)
        mark_event_completed(event_id)
    except Exception as exc:
        print(f"Event {event_id} failed: {exc}")
        mark_event_failed(event_id, str(exc), retryable=True)


def worker_loop(worker_number: int):
    while not STOP_EVENT.is_set():
        event_id = None
        try:
            event_id = MESSAGE_QUEUE.get(timeout=0.5)
        except queue.Empty:
            event_id = claim_next_pending_event()

        if event_id is None:
            continue

        # Queue item may already have been claimed by a polling worker.
        with DB_LOCK:
            conn = get_db_connection()
            try:
                row = conn.execute(
                    "SELECT status FROM webhook_events WHERE id=?",
                    (event_id,),
                ).fetchone()
            finally:
                conn.close()
        if not row or row[0] not in {"queued", "processing"}:
            MESSAGE_QUEUE.task_done()
            continue

        # Queue items are initially marked queued. Transition to processing exactly once.
        if row[0] == "queued":
            claimed = False
            with DB_LOCK:
                conn = get_db_connection()
                try:
                    claimed = bool(
                        conn.execute(
                            """
                            UPDATE webhook_events
                            SET status='processing', claimed_at=?
                            WHERE id=? AND status='pending'
                            """,
                            (time.time(), event_id),
                        ).rowcount
                    )
                    conn.commit()
                finally:
                    conn.close()
            if not claimed:
                MESSAGE_QUEUE.task_done()
                continue

        try:
            process_event_record(event_id)
        finally:
            MESSAGE_QUEUE.task_done()


def start_workers():
    reset_stale_processing_events()
    if WORKERS:
        return
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
                    continue
                enqueue_webhook_event(sender_id, message_id, messaging_event)

    return "EVENT_RECEIVED", 200


# ========================================================
# 🚀 Startup
# ========================================================
init_db()
start_workers()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
