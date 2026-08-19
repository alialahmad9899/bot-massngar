# تشغيل البوت على Render بذاكرة 512MB

## Environment Variables المطلوبة

- `GEMINI_API_KEY`
- `PAGE_ACCESS_TOKEN`
- `VERIFY_TOKEN`
- `APP_SECRET`
- `ADMIN_PASSWORD`

لا يوجد Default آمن لأي Secret. التطبيق يرفض الإقلاع إذا نقص أحدها.

## أمر التشغيل المقترح

```bash
gunicorn academy_bot_production:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 30
```

استخدم Worker واحد فقط في Render 512MB، لأن التطبيق نفسه يشغّل عاملَي معالجة داخليين بحد أقصى للحفاظ على ترتيب الرسائل وتقليل استهلاك RAM.

## ملاحظة مهمة عن SQLite

النسخة تستخدم SQLite WAL وInbox داخلياً لتقليل فقدان الرسائل عند امتلاء Queue أو حدوث أخطاء مؤقتة.
للاستمرار بعد إعادة إنشاء Instance في Render، يجب وضع `DB_PATH` على Render Persistent Disk، مثلاً:

```text
/mnt/data/academy_bot.db
```

ثم ضبط:

```text
DB_PATH=/mnt/data/academy_bot.db
```

بدون Persistent Disk، أي قاعدة SQLite على filesystem المؤقتة في Render قد تضيع عند إعادة إنشاء الـInstance.
