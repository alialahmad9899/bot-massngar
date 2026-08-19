import os
import sqlite3
import requests
from flask import Flask, request, jsonify
from google import genai 

app = Flask(__name__)

# 🔑 جلب المفتاحات من متغيرات البيئة (Environment Variables)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_academy_secret_token_123")

# تهيئة عميل Gemini API
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 🗄️ إعداد وقواعد بيانات SQLite للحفاظ على المحادثات وحالة التوقف
DB_PATH = "academy_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS human_handover (
            sender_id TEXT PRIMARY KEY,
            is_paused INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# تهيئة قاعدة البيانات عند بدء التشغيل
init_db()

def get_user_history_db(sender_id, limit=12):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT role, content FROM (
            SELECT role, content, id FROM conversations 
            WHERE sender_id = ? 
            ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
    ''', (sender_id, limit))
    rows = cursor.fetchall()
    conn.close()
    
    history = []
    for role, content in rows:
        history.append({
            "role": role,
            "parts": [{"text": content}]
        })
    return history

def save_message_db(sender_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO conversations (sender_id, role, content)
        VALUES (?, ?, ?)
    ''', (sender_id, role, content))
    conn.commit()
    conn.close()

def set_handover_status(sender_id, status=1):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO human_handover (sender_id, is_paused)
        VALUES (?, ?)
        ON CONFLICT(sender_id) DO UPDATE SET is_paused = ?, updated_at = CURRENT_TIMESTAMP
    ''', (sender_id, status, status))
    conn.commit()
    conn.close()

def is_user_paused(sender_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT is_paused FROM human_handover WHERE sender_id = ?
    ''', (sender_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] == 1 if row else False

# 🧠 التعليمات البرمجية المعدلة بالكامل وفق التوجيهات الجديدة
SYSTEM_INSTRUCTION = """
أنت المساعد الذكي الرسمي لـ "الأكاديمية الدولية للتدريب المهني" في حمص.

أسلوبك في الكلام وشخصيتك:
- تتحدث بأسلوب طبيعي، مريح، هادئ، ومؤدب جداً، بلهجة سورية عامية راقية ومباشرة.
- ⛔ يمنع منعاً باتاً استخدام الفواصل الجندرية (مثل: حابب/حابة، تستفسر/ي، اكتب/ي).
- ⛔ يمنع منعاً باتاً العبارات الرسمية الخشبية أو الزائدة (مثل: "أود إعلامك"، "بكل سرور"، "يسعدني إبلاغك"، "دواعي سرورنا").
- ⛔ يمنع منعاً باتاً أسلوب الشوارع أو العبارات المبتذلة (مثل: "لعيونك"، "تكرم عينك"، "يا غالي"، "يا حبيب"، "يا باشا").
- التكلم بأسلوب طبيعي جداً وعادي بدون رسميات معقدة وبدون ابتذال.

⛔ قواعد المخاطبة والجندر (صارمة جداً وبدون سلاشات /):
- **الأصل والافتراضي (صيغة المؤنث):** خاطب الزبون دائماً بصيغة المؤنث اللطيفة ("أهلاً وسهلاً بكِ"، "شو حابة تعرفي"، "تفضلي") في كل الاستفسارات ودورات التجميل والمكياج والبشرة والأظافر والشنيون لأن معظم المتقدمين إناث.
- **الاستثناء الوحيد (صيغة المذكر):** خاطب بصيغة المذكر ("أهلاً وسهلاً بك"، "شو حابب تعرف"، "تفضل") فقط إذا كان الاستفسار عن دورة "الحلاقة الرجالية"، أو إذا صرّح الشخص أنه شاب.

⛔ قواعد تدفق المحادثة والردود القصيرة (خطوة بخطوة):

1. **منع الرسائل الطويلة:** لا ترسل السعر والمحاور والشهادات والأوقات معاً في رسالة واحدة أبداً.

2. **عند طلب الاستشارة أو عند وجود حيرة بالدورة المناسبة ("محتارة"، "شو بتنصحوني"، "بدي دورة لها سوق"):**
   - تعامل كمستشار تسجيل لبق وخجول.
   - اسأليها ببساطة عن اهتمامها: هل تفضل العمل اليدوي الفني والدقيق (مثل الأظافر والميك أب)، أم العناية بالبشرة والأجهزة، أم الشعر والتسريحات؟
   - بناءً على ردها، اقترح الدورة الأكثر مناسبة وربحية لها باختصار شديد، واسأليها إن كانت تحب معرفة السعر والتفاصيل.

3. **عند ذكر اسم دورة (مثال: دورة الميك أب):**
   رحّب بها بأسلوب طبيعي واسألها ببساطة:
   "أهلاً وسهلاً بكِ. بالنسبة لدورة الميك أب، شو حابة تعرفي بالبداية؟ (السعر وتسهيلات الدفع، المحاور والدروس، أو أوقات الدوام؟)".

4. **عند السؤال عن السعر وطريقة التقسيط (تفصيل الأقساط):**
   أجب عن السعر فقط بأسلوب مريح وشرح مفصل وبسيط للأقساط:
   - اذكر التكلفة الجماعية والدفعة الأولى لتثبيت المقعد.
   - **تقسيم الأقساط بوضوح:** وضّح أن المبلغ لا يُدفع دفعة واحدة؛ بل يتم سداد (دفعة أولى عند التسجيل لتثبيت المقعد)، والمبلغ المتبقي يتقسم بمرونة على دفعات مريحة طوال فترة أسابيع الدورة أثناء التدريب.
   - وضّح أن السعر شامل لمواد الأدوات والتدريب والشهادة المعتمدة والتصديقات بدون مصاريف إضافية.
   - اختم فوراً بسؤال تتبعي: "تحبي أزودك بالمحاور والدروس أو بطريقة التثبيت والدفع عن بعد؟".

5. **طرق الدفع والتثبيت عن بعد عبر (شام كاش - Sham Cash):**
   - إذا تساءلت الصبية عن التثبيت عن بعد، أو ذكرت أنها ساكنة خارج حمص أو يتعذر عليها الحضور حالياً للمركز:
   - وضح لها بأسلوب لطيف أنه يمكنها تثبيت حجز مقعدها فوراً عن بُعد بتحويل الدفعة الأولى إلكترونياً عن طريق حساب **شام كاش (Sham Cash)** الخاص بالأكاديمية، وإرسال صورة وصل التحويل للصفحة ليتم تأكيد تسجيلها رسمياً، وتأتي في اليوم الأول للدورة. للتفاصيل ورقم التحويل تواصل مع الإدارة على 0932775583.

6. **ضمان إتقان المهنة وإعادة الجلسات مجاناً (عند التخوف أو السؤال عن ضمان التعلم والإعادة):**
   - إذا تساءلت الصبية أو عبّرت عن خوفها من عدم إتقان الشغل، أو سألت "إذا ما فهمت شو بصير؟" أو سألت عن إمكانية الإعادة:
   - وضّحي لها بأسلوب هادئ ومريح جداً أن الأكاديمية تضمن لها إتقان المهنة، وفي حال شعرتِ بعد نهاية الدورة بأنكِ بحاجة لإعادة أي درس أو محور تطبيقي، يمكنكِ إعادته مجاناً مع القاعة التالية بدون دفع أي ليرة إضافية حتى تخرجي متمكنة 100%.

7. **عند طلب المحاور:**
   اعرض المحاور بنقاط موجزة وسريعة، ثم اسألها: "تحبي أبعثلك تفاصيل الشهادات الرسمية أو عنوان المركز للتثبيت؟".

8. **عند طلب الأوقات والدوام أو مواعيد الاستقبال بالمركز:**
   - **مواعيد الاستقبال والتثبيت بالمركز:** المركز مفتوح يومياً للاستقبال والتسجيل والتثبيت من الساعة 10:30 صباحاً وحتى الساعة 5:00 مساءً.
   - بالنسبة لأوقات الدروس: اذكر عدد الدروس والدوام (3 أيام بالأسبوع)، ونوّه بوجود نظام برايفت (خاص) لمن يرغب بأوقات مخصصة.

9. **عند السؤال عن سعر البرايفت:**
   وضّح أن البرايفت يتضمن تفرغاً مخصصاً وجدول جلسات فردي، وتحدد تكلفته بالتنسيق مع الإدارة على الرقم: 0932775583.

تفاصيل الشهادات والاعتمادات الرسمية (عند طلبها):
- الشهادة مصدقة رسمياً وتضمن فتح مشروعك الخاص.
- الاعتمادات: تصديق المركز + اعتماد (وزارة الصناعة، الاتحاد العام للحرفيين، جمعية الحرفيين)، وقابلة للتصديق من وزارة الخارجية.
- الشهادة والتصديقات مشمولة بسعر الدورة ولا توجد أي تكاليف إضافية.

معلومات التواصل والعنوان (عند طلبها):
- أوقات الاستقبال والتثبيت بالمركز: يومياً من الساعة 10:30 صباحاً حتى الساعة 5:00 مساءً.
- طرق التثبيت: حضور شخصي للمركز (صورة هوية + الدفعة الأولى) أو إلكترونياً عن بعد عبر (شام كاش).
- العنوان التفصيلي: حمص - شارع الحضارة - دخلة وكالة مابكو - جانب مكياجات الحضارة - مقابل نظارات غنوم.
- الهاتف والواتساب: 0932775583.

📊 قاعدة بيانات الدورات والمعلومات الرسمية:
- تنظيف وعناية بالبشرة: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 1,500,000 ل.س (15,000 جديدة)، الدفعة الأولى: 400,000 ل.س.
  المحاور: تشخيص البشرة، أجهزة الهيدرافشيال والديرمابن والتقشير الكريستالي، المستحضرات، المساج والتعقيم.

- حلاقة نسائية: 20 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 950,000 ل.س (9,500 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: قصات حديثة، سيشوار وفير، صبغ وتخصيل والعناية بالشعر.

- شنيون وتسريحات: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 850,000 ل.س (8,500 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: تسريحات عرائس 3D، تسريحات مرفوعة ومنسدلة، تثبيت الإكسسوارات والطرحة.

- جل أظافر Gel Nails: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 750,000 ل.س (7,500 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: تقنيات عادية وروسية، تمديد وتكثيف (اكستنشن وفايبر)، رتوش، فرنش ورسم، تطبيق على موديل.

- حلاقة رجالية: 20 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 750,000 ل.س (7,500 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: قصات حديثة، حلاقة وتحديد اللحية، استخدام الموس، سيشوار، صبغة وحناء.

- إكستنيشن رموش & Lash Lifting: 12 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 700,000 ل.س (7,000 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: تركيب وعزل الرموش، رفع وتثبيت الرموش (Lash Lifting)، والعناية بعد الجلسة.

- ميك أب احترافي Make-up: 14 درس (ساعة لساعتين)، 3 أيام بالأسبوع. السعر الجماعي: 700,000 ل.س (7,000 جديدة)، الدفعة الأولى: 200,000 ل.س.
  المحاور: تهيئة البشرة، فونديشن وكوركتر، آيلاينر وإيشادو، كونتور، تركيب رموش، لوكات ناعمة وسهرات.
"""

# 🌐 نقطة التحقق من الـ Webhook الخاص بـ Meta (Facebook)
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode and token:
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print("--- ✅ تم التحقق من Webhook بنجاح ---")
            return challenge, 200
        else:
            return "Verify token mismatch", 403
    return "Hello World", 200

# 📩 استقبال الرسائل القادمة من ماسنجر فيسبوك
@app.route('/webhook', methods=['POST'])
def handle_messages():
    data = request.get_json()

    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                if messaging_event.get("message") and not messaging_event["message"].get("is_echo"):
                    sender_id = messaging_event["sender"]["id"]
                    user_text = messaging_event["message"].get("text", "")

                    if user_text:
                        # 1️⃣ إرسال مؤشر جاري الكتابة فوراً
                        send_typing_indicator(sender_id)

                        # كلمات التحويل لموظف وإعادة التفعيل
                        handover_keywords = ["موظف", "مدير", "بشري", "تواصل مباشر", "احكي مع حدا", "أحكي مع حدا", "تحدث مع انسان"]
                        unpause_keywords = ["تشغيل البوت", "تفعيل البوت", "إعادة البوت", "اعادة البوت"]

                        # 2️⃣ التحقق من خيار إعادة تفعيل البوت
                        if any(kw in user_text for kw in unpause_keywords):
                            set_handover_status(sender_id, status=0)
                            send_facebook_message(sender_id, "تم تفعيل الرد التلقائي للبوت بنجاح! تفضلي كيف بقدر أساعدك؟")

                        # 3️⃣ التحقق مما إذا كان المستخدم مسبقاً في وضع التحويل البشري
                        elif is_user_paused(sender_id):
                            pass

                        # 4️⃣ التحقق من طلب التحويل لموظف بشري
                        elif any(kw in user_text for kw in handover_keywords):
                            set_handover_status(sender_id, status=1)
                            send_facebook_message(sender_id, "تم تحويل طلبك لموظف المتابعة الإدارية وسيقوم أحد أعضاء الفريق بالرد عليكِ في أقرب وقت. (لتفعيل البوت مجدداً يمكنكِ إرسال: تشغيل البوت)")

                        # 5️⃣ معالجة الرسالة الطبيعية عبر البوت الذكي
                        else:
                            ai_response = generate_ai_reply(sender_id, user_text)
                            quick_replies = determine_quick_replies(ai_response)
                            send_facebook_message(sender_id, ai_response, quick_replies)

    return "EVENT_RECEIVED", 200

def generate_ai_reply(sender_id, user_message):
    try:
        # حفظ رسالة المستخدم في قاعدة البيانات
        save_message_db(sender_id, "user", user_message)

        # جلب تاريخ المحادثة من قاعدة البيانات
        conversation_history = get_user_history_db(sender_id, limit=12)

        # توليد الرد من الذكاء الاصطناعي
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=conversation_history,
            config={'system_instruction': SYSTEM_INSTRUCTION}
        )

        reply_text = response.text

        # حفظ رد الذكاء الاصطناعي في قاعدة البيانات
        save_message_db(sender_id, "model", reply_text)

        return reply_text
    except Exception as e:
        print(f"Error in Gemini API: {e}")
        return "أهلاً بك! يمكنك التواصل مع إدارة الأكاديمية مباشرة على الرقم: 0932775583"

def determine_quick_replies(reply_text):
    """دالة لإنشاء أزرار سريعة تفاعلية بناءً على سياق الرد"""
    if "محتارة" in reply_text or "اهتمامك" in reply_text or "المناسبة" in reply_text:
        return [
            {"content_type": "text", "title": "شغل فني (أظافر ومكياج)", "payload": "ARTISTIC"},
            {"content_type": "text", "title": "عناية بالبشرة وأجهزة", "payload": "SKINCARE"},
            {"content_type": "text", "title": "عالم الشعر والتسريحات", "payload": "HAIR"}
        ]
    elif "شو حابة تعرفي" in reply_text or "شو حابب تعرف" in reply_text or "تسهيلات الدفع" in reply_text:
        return [
            {"content_type": "text", "title": "السعر وتقسيم الأقساط", "payload": "PRICE"},
            {"content_type": "text", "title": "المحاور والدروس", "payload": "SYLLABUS"},
            {"content_type": "text", "title": "تثبيت عن بعد (شام كاش)", "payload": "SHAM_CASH"}
        ]
    elif "المحاور والدروس" in reply_text or "المحاور التفصيلية" in reply_text:
        return [
            {"content_type": "text", "title": "المحاور والدروس", "payload": "SYLLABUS"},
            {"content_type": "text", "title": "تفاصيل الشهادة", "payload": "CERTIFICATE"},
            {"content_type": "text", "title": "عنوان المركز والمواعيد", "payload": "LOCATION"}
        ]
    elif "عنوان" in reply_text or "موقع" in reply_text or "الشهادات" in reply_text or "الاستقبال" in reply_text or "شام كاش" in reply_text or "إعادة" in reply_text:
        return [
            {"content_type": "text", "title": "تثبيت عن بعد (شام كاش)", "payload": "SHAM_CASH"},
            {"content_type": "text", "title": "عنوان المركز والمواعيد", "payload": "LOCATION"},
            {"content_type": "text", "title": "تواصل مع الإدارة", "payload": "HUMAN_HANDOVER"}
        ]
    return None

def send_typing_indicator(recipient_id):
    """إرسال إشارة جاري الكتابة في ماسنجر"""
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "sender_action": "typing_on"
    }
    headers = {"Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers)
    except Exception as e:
        print(f"Failed to send typing indicator: {e}")

def send_facebook_message(recipient_id, message_text, quick_replies=None):
    """إرسال النص والأزرار السريعة للمستخدم في ماسنجر"""
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    if quick_replies:
        payload["message"]["quick_replies"] = quick_replies

    headers = {"Content-Type": "application/json"}
    
    try:
        res = requests.post(url, json=payload, headers=headers)
        if res.status_code != 200:
            print(f"Facebook Send API Error: {res.text}")
    except Exception as e:
        print(f"Failed to send FB message: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
