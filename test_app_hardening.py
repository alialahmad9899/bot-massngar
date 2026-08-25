import hashlib
import hmac
import importlib.util
import os
import sqlite3
from pathlib import Path


def app_path() -> str:
    return str(Path(__file__).with_name("app.py"))


def load_module(path: str | None = None):
    import sys, types
    flask = types.ModuleType('flask')
    class DummyRequest: pass
    flask.request = DummyRequest()
    flask.Flask = lambda name: type('DummyApp', (), {'route': lambda self, *a, **k: (lambda f: f), 'run': lambda *a, **k: None})()
    sys.modules['flask'] = flask
    requests = types.ModuleType('requests')
    requests.RequestException = Exception
    class DummySession:
        def post(self, *a, **k): return type('R', (), {'status_code': 200, 'text': ''})()
        def get(self, *a, **k): return type('R', (), {'status_code': 200, 'text': '', 'json': lambda self: {'data': []}})()
    requests.Session = DummySession
    sys.modules['requests'] = requests
    google = types.ModuleType('google')
    genai = types.ModuleType('google.genai')
    class DummyClient:
        def __init__(self, *a, **k): pass
    genai.Client = DummyClient
    google.genai = genai
    sys.modules['google'] = google
    sys.modules['google.genai'] = genai
    os.environ['GEMINI_API_KEY'] = 'test-gemini'
    os.environ['PAGE_ACCESS_TOKEN'] = 'test-page'
    os.environ['VERIFY_TOKEN'] = 'test-verify'
    os.environ['APP_SECRET'] = 'test-app-secret'
    os.environ['ADMIN_PASSWORD'] = 'test-admin-password'
    spec = importlib.util.spec_from_file_location('academy_bot_test', path or app_path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_production_secrets_are_required():
    text = Path(app_path()).read_text(encoding='utf-8')
    assert 'ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")' in text
    assert 'APP_SECRET = os.environ.get("APP_SECRET")' in text
    assert 'VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")' in text
    assert 'PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")' in text
    assert 'CRITICAL ERROR: Missing required environment variables:' in text


def test_meta_signature_is_verified_against_raw_body():
    mod = load_module()
    secret = b'test-app-secret'
    body = b'{"object":"page","entry":[]}'
    expected = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert mod.verify_signature_bytes(body, expected) is True
    assert mod.verify_signature_bytes(body, 'sha256=' + '0' * 64) is False
    assert mod.verify_signature_bytes(body, '') is False


def test_deduplication_is_atomic_and_ttl_prunes():
    mod = load_module()
    mod.PROCESSED_MESSAGES.clear()
    assert mod.is_duplicate_message('m1') is False
    assert mod.is_duplicate_message('m1') is True
    mod.PROCESSED_MESSAGES['old'] = 0
    mod.prune_dedup_cache(now=90000)
    assert 'old' not in mod.PROCESSED_MESSAGES


def test_admin_compare_and_lockout():
    mod = load_module()
    mod.ADMIN_ATTEMPTS.clear()
    assert mod.verify_admin_password('test-admin-password') is True
    assert mod.verify_admin_password('wrong') is False
    for _ in range(3):
        mod.record_admin_attempt('u1', success=False, now=1000)
    locked, remaining = mod.check_admin_lockout('u1', now=1001)
    assert locked is True
    assert 898 <= remaining <= 900


def test_bounded_queue_does_not_grow_without_limit():
    mod = load_module()
    assert mod.MESSAGE_QUEUE.maxsize == 100


def test_sqlite_schema_has_webhook_inbox(tmp_path):
    mod = load_module()
    mod.DB_PATH = str(tmp_path / 'academy.sqlite3')
    mod.init_db()
    conn = sqlite3.connect(mod.DB_PATH)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert 'webhook_events' in tables
