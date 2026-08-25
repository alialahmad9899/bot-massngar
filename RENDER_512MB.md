# تشغيل البوت على Render بذاكرة 512MB

## Environment Variables المطلوبة

- `GEMINI_API_KEY`
- `PAGE_ACCESS_TOKEN`
- `VERIFY_TOKEN`
- `APP_SECRET`
- `ADMIN_PASSWORD`

لا يوجد Default آمن لأي Secret. التطبيق يرفض الإقلاع إذا نقص أحدها.

## أمر التشغيل المعتمد

```bash
gunicorn --workers 1 --threads 2 app_entry:app
```

لا تستخدم `app:app` في الإنتاج الجديد؛ `app_entry.py` يحمّل الـUnified Production Runtime قبل استقبال الطلبات.

Render يعمل بWorker Gunicorn واحد، بينما التطبيق نفسه يشغّل عاملَي معالجة داخليين كحد أقصى للحفاظ على ترتيب الرسائل وتقليل استهلاك RAM.

## SQLite والاستمرارية

التطبيق يستخدم SQLite WAL وDurable Webhook Inbox وIdempotent Response Tracking.
إذا كان Render Persistent Disk مركباً على `/var/data`، سينقل runtime قاعدة البيانات إلى:

```text
/var/data/academy_bot.db
```

ويستخدمها تلقائياً.

إذا لم يكن هناك Persistent Disk، تبقى SQLite على filesystem المؤقتة في Render، وبالتالي يمكن أن تضيع المحادثات والدورات والمواعيد وCRM عند إعادة إنشاء Instance.

## اختبارات النشر

قبل اعتبار الإصدار Production-ready، يجب أن يكون GitHub Actions أخضر لكل:

- Hardening
- Admin/CRM
- Language/Conversation
- Human Handover
- Unified Production Runtime

كما يجب بعد Deploy التحقق من:

```text
GET /health
POST /webhook
```

ومراقبة Logs الخاصة بـ:

```text
[RUNTIME] unified v2 ready
[WORKER-1] started
[WORKER-2] started
[WEBHOOK] durable
[EVENT] completed
```
