import os, tempfile, unittest, sys, types
os.environ.setdefault('GEMINI_API_KEY','test')
os.environ.setdefault('PAGE_ACCESS_TOKEN','test')
os.environ.setdefault('VERIFY_TOKEN','test')
os.environ.setdefault('APP_SECRET','test')
os.environ.setdefault('ADMIN_PASSWORD','secret')
os.environ['DB_PATH'] = os.path.join(tempfile.gettempdir(), 'academy_admin_tools_test.db')
os.environ['BOT_AUTO_START_WORKERS'] = '0'
try:
    os.remove(os.environ['DB_PATH'])
except FileNotFoundError:
    pass

# Lightweight import stubs so admin-tool tests can run without network-installed web SDKs.
flask_stub = types.ModuleType('flask')
class FakeFlask:
    def __init__(self, *a, **k): pass
    def route(self, *a, **k):
        return lambda fn: fn
flask_stub.Flask = FakeFlask
flask_stub.request = types.SimpleNamespace()
sys.modules['flask'] = flask_stub

requests_stub = types.ModuleType('requests')
class FakeSession:
    def post(self, *a, **k): raise RuntimeError('not used in unit tests')
requests_stub.Session = FakeSession
requests_stub.RequestException = Exception
sys.modules['requests'] = requests_stub

google_stub = types.ModuleType('google')
genai_stub = types.ModuleType('google.genai')
class FakeClient:
    def __init__(self, *a, **k): pass
genai_stub.Client = FakeClient
google_stub.genai = genai_stub
sys.modules['google'] = google_stub
sys.modules['google.genai'] = genai_stub

import app

class AdminToolsTests(unittest.TestCase):
    def setUp(self):
        app.init_db()
        app.clear_admin_store_for_tests()

    def test_add_course_and_retrieve_it(self):
        result = app.admin_execute('admin1', 'أضف دورة ميك أب متقدم، 16 درس، السعر 900000، الدفعة الأولى 250000، تبدأ 2026-09-01')
        self.assertTrue(result['ok'], result)
        courses = app.admin_list_courses()
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]['name'], 'ميك أب متقدم')
        self.assertEqual(courses[0]['price'], 900000)
        self.assertEqual(courses[0]['start_date'], '2026-09-01')

    def test_update_course_price_and_start_date(self):
        app.admin_execute('admin1', 'أضف دورة تنظيف البشرة، 12 درس، السعر 500000')
        result = app.admin_execute('admin1', 'عدّل دورة تنظيف البشرة، السعر 650000، تبدأ 2026-10-10')
        self.assertTrue(result['ok'], result)
        course = app.admin_list_courses()[0]
        self.assertEqual(course['price'], 650000)
        self.assertEqual(course['start_date'], '2026-10-10')

    def test_generic_knowledge_store(self):
        result = app.admin_execute('admin1', 'أضف معلومة الدفع الإلكتروني = عبر شام كاش من حساب الأكاديمية')
        self.assertTrue(result['ok'], result)
        value = app.get_dynamic_knowledge('الدفع الإلكتروني')
        self.assertEqual(value, 'عبر شام كاش من حساب الأكاديمية')

    def test_add_and_list_batch(self):
        app.admin_execute('admin1', 'أضف دورة حلاقة رجالية، 20 درس، السعر 750000')
        result = app.admin_execute('admin1', 'أضف موعد بدء لدورة حلاقة رجالية: 2026-09-15')
        self.assertTrue(result['ok'], result)
        batches = app.admin_list_batches()
        self.assertEqual(batches[0]['start_date'], '2026-09-15')

    def test_natural_setting_update_reaches_dynamic_knowledge(self):
        result = app.admin_execute('admin1', 'غيّر رقم الواتساب إلى 0999999999')
        self.assertTrue(result['ok'], result)
        value = app.get_dynamic_knowledge('رقم الواتساب')
        self.assertEqual(value, '0999999999')
        self.assertIn('رقم الواتساب', app.build_dynamic_academy_knowledge())

    def test_sensitive_delete_requires_confirmation(self):
        app.admin_execute('admin1', 'أضف معلومة سياسة الاسترجاع = لا يوجد استرجاع')
        result = app.admin_execute('admin1', 'احذف معلومة سياسة الاسترجاع')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'CONFIRM_REQUIRED')
        result2 = app.confirm_admin_action('admin1')
        self.assertTrue(result2['ok'])
        self.assertIsNone(app.get_dynamic_knowledge('سياسة الاسترجاع'))

if __name__ == '__main__':
    unittest.main()
