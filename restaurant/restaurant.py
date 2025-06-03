import sys
import json 
import time
import logging
import uuid
import re
import os
import traceback
import datetime
import aiomysql
import pymysql
import asyncio
from asyncio import Lock
import nest_asyncio
from telegram.error import TelegramError
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, CallbackContext
from contextlib import asynccontextmanager
from telegram.ext import ContextTypes
from telegram.request import HTTPXRequest
from collections import deque
from telegram.error import NetworkError

order_rate_lock = Lock()
last_order_time = 0  # بالثواني



async def track_sent_message(message_id, order_id, source, destination, content):
    """تتبع الرسائل المرسلة في قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO message_tracking 
                    (message_id, order_id, source, destination, content, sent_time) 
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    """,
                    (message_id, order_id, source, destination, content)
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.error(f"خطأ في تتبع الرسالة: {e}")
        return False



# قفل التزامن للطلبات
order_locks = {}
order_queue = asyncio.Queue()

async def get_order_lock(order_id):
    """الحصول على قفل خاص بطلب معين"""
    if order_id not in order_locks:
        order_locks[order_id] = asyncio.Lock()
    return order_locks[order_id]


# دالة لإضافة طلب إلى قائمة الانتظار
async def enqueue_order(order_data):
    await order_queue.put(order_data)

# دالة لمعالجة الطلبات من قائمة الانتظار
async def process_order_queue():
    while True:
        order_data = await order_queue.get()
        try:
            await process_order(order_data)  # يجب أن تكتب أنت logic المعالجة أو تستدعي موجوداً
        except Exception as e:
            logger.error(f"خطأ في معالجة الطلب: {e}")
        finally:
            order_queue.task_done()
        await asyncio.sleep(0.1)


# محدد معدل الطلبات
# محدد معدل الطلبات
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    async def acquire(self):
        now = time.time()
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            wait_time = self.calls[0] + self.period - now
            await asyncio.sleep(wait_time)
        self.calls.append(time.time())

telegram_limiter = RateLimiter(max_calls=30, period=1)

async def send_message_with_rate_limit(bot, chat_id, text, **kwargs):
    await telegram_limiter.acquire()
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


# دالة لإرسال رسالة مع إعادة المحاولة + Rate Limiting
async def send_message_with_retry(bot, chat_id, text, order_id=None, max_retries=5, **kwargs):
    message_id = str(uuid.uuid4())

    for attempt in range(max_retries):
        try:
            # ✅ التحكم بمعدل الإرسال
            await telegram_limiter.acquire()

            # ✅ إزالة أي مفاتيح غير مدعومة
            kwargs.pop("message_id", None)

            # ✅ إرسال الرسالة
            sent_message = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return sent_message

        except Exception as e:
            logger.error(f"فشل في إرسال الرسالة (المحاولة {attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(0.5 * (2 ** attempt))  # تصاعد زمني

    raise Exception(f"فشلت جميع المحاولات لإرسال الرسالة.")


async def start_order_queue_processor():
    while True:
        try:
            order_id, callback = await order_queue.get()
            await callback(order_id)
        except Exception as e:
            logger.error(f"❌ خطأ في start_order_queue_processor: {e}")
            await asyncio.sleep(1)





# دالة لتحديث حالة الطلب في قاعدة البيانات المشتركة
async def update_order_status(order_id, status, bot_type):
    async with get_db_connection() as conn:
        async with conn.cursor() as cursor:
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # تحديث الحالة وتوقيت آخر مزامنة حسب نوع البوت
            if bot_type == "user":
                await cursor.execute(
                    "INSERT INTO order_status (order_id, status, last_sync_user_bot) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE status = %s, last_sync_user_bot = %s",
                    (order_id, status, current_time, status, current_time)
                )
            else:  # restaurant
                await cursor.execute(
                    "INSERT INTO order_status (order_id, status, last_sync_restaurant_bot) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE status = %s, last_sync_restaurant_bot = %s",
                    (order_id, status, current_time, status, current_time)
                )
            
        await conn.commit()




# ✅ مسار قاعدة البيانات
DB_PATH = "restaurant_orders.db"

@asynccontextmanager
async def get_db_connection():
    conn = await aiomysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        port=DB_PORT,
        charset='utf8mb4',
        autocommit=False
    )
    try:
        yield conn
    finally:
        conn.close()

        
async def initialize_database():
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            charset='utf8mb4'
        )
        cursor = conn.cursor()

        # إنشاء قاعدة البيانات إذا لم تكن موجودة
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cursor.execute(f"USE {DB_NAME}")

        # إنشاء جدول الطلبات
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id VARCHAR(255),
                order_number INT,
                restaurant VARCHAR(255),
                total_price INT,
                timestamp DATETIME
            )
        """)

        # إنشاء جدول الدليفري
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS delivery_persons (
                id INT AUTO_INCREMENT PRIMARY KEY,
                restaurant_id INT NOT NULL,
                name VARCHAR(255) NOT NULL,
                phone VARCHAR(20) NOT NULL,
                FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE
            )
        """)

        conn.commit()
        conn.close()

        logger.info("✅ تم التأكد من وجود جدول الطلبات وجدول الدليفري.")

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إنشاء الجداول: {e}")



# 🔹 إعداد سجل الأخطاء
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)




if len(sys.argv) < 2:
    print("❌ يرجى تمرير اسم ملف الإعداد: مثال ➜ python3 restaurant.py Almalek")
    sys.exit(1)

restaurant_key = sys.argv[1]
CONFIG_FILE = f"config/{restaurant_key}.json"

with open(CONFIG_FILE, encoding="utf-8") as f:
    config = json.load(f)

TOKEN = config["token"]
CHANNEL_ID = config["channel_id"]
CASHIER_CHAT_ID = config["cashier_id"]
RESTAURANT_COMPLAINTS_CHAT_ID = config["complaints_channel_id"]
RESTAURANT_ID = config["restaurant_id"]
RESTAURANT_NAME = config["restaurant_name"]





# إضافة هذه المتغيرات
DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "botuser"
DB_PASSWORD = "strongpassword123"
DB_NAME = "telegram_bot"



# 🔹 إدارة الطلبات المؤقتة
pending_orders = {}
pending_locations = {}


# حفظ الطلب المؤقت في قاعدة البيانات
async def save_pending_order(order_id, order_details, channel_message_id, cashier_message_id, location=None):
    async with get_db_connection() as conn:
        async with conn.cursor() as cursor:
            if location:
                latitude, longitude = location
                await cursor.execute(
                    "INSERT INTO pending_orders (order_id, order_details, channel_message_id, cashier_message_id, location_latitude, location_longitude) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE order_details = %s, channel_message_id = %s, cashier_message_id = %s, location_latitude = %s, location_longitude = %s",
                    (order_id, order_details, channel_message_id, cashier_message_id, latitude, longitude,
                     order_details, channel_message_id, cashier_message_id, latitude, longitude)
                )
            else:
                await cursor.execute(
                    "INSERT INTO pending_orders (order_id, order_details, channel_message_id, cashier_message_id) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE order_details = %s, channel_message_id = %s, cashier_message_id = %s",
                    (order_id, order_details, channel_message_id, cashier_message_id,
                     order_details, channel_message_id, cashier_message_id)
                )
        await conn.commit()


# استرجاع الطلبات المؤقتة من قاعدة البيانات عند بدء تشغيل البوت
async def load_pending_orders():
    async with get_db_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM pending_orders")
            rows = await cursor.fetchall()

            for row in rows:
                order_id = row[0]
                order_details = row[1]
                channel_message_id = row[2]
                cashier_message_id = row[3]

                pending_orders[order_id] = {
                    "order_details": order_details,
                    "channel_message_id": channel_message_id,
                    "message_id": cashier_message_id
                }

                if row[4] and row[5]:
                    pending_orders[order_id]["location"] = (row[4], row[5])










# دالة حفظ حالة المحادثة الموحدة
async def save_conversation_state(user_id, state_data):
    """حفظ حالة المحادثة في قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # تحويل البيانات إلى JSON
                json_data = json.dumps(state_data, ensure_ascii=False)

                # استخدام REPLACE INTO لإضافة أو تحديث البيانات
                await cursor.execute(
                    "REPLACE INTO conversation_states (user_id, state_data) VALUES (%s, %s)",
                    (user_id, json_data)
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.error(f"خطأ في حفظ حالة المحادثة: {e}")
        return False

# دالة استرجاع حالة المحادثة الموحدة
async def get_conversation_state(user_id):
    """استرجاع حالة المحادثة من قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT state_data FROM conversation_states WHERE user_id = %s",
                    (user_id,)
                )
                result = await cursor.fetchone()

                if not result:
                    return {}

                return json.loads(result[0])
    except Exception as e:
        logger.error(f"خطأ في استرجاع حالة المحادثة: {e}")
        return {}

# دالة إنشاء طلب جديد
async def create_order(user_id, restaurant_id, items, total_price):
    """إنشاء طلب جديد في قاعدة البيانات"""
    try:
        order_id = str(uuid.uuid4())

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # إنشاء الطلب الرئيسي
                await cursor.execute(
                    "INSERT INTO orders (order_id, user_id, restaurant_id, total_price) VALUES (%s, %s, %s, %s)",
                    (order_id, user_id, restaurant_id, total_price)
                )

                # إضافة عناصر الطلب
                for item in items:
                    await cursor.execute(
                        "INSERT INTO order_items (order_id, meal_id, quantity, price, options) VALUES (%s, %s, %s, %s, %s)",
                        (order_id, item['meal_id'], item['quantity'], item['price'], json.dumps(item.get('options', {})))
                    )

            await conn.commit()
        return order_id
    except Exception as e:
        logger.error(f"خطأ في إنشاء طلب جديد: {e}")
        return None

# دالة تحديث حالة الطلب
async def update_order_status(order_id, status):
    """تحديث حالة الطلب في قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE orders SET status = %s WHERE order_id = %s",
                    (status, order_id)
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.error(f"خطأ في تحديث حالة الطلب: {e}")
        return False

#_______________________

# دوال إنشاء الرسائل الموحدة
def create_order_accepted_message(order_id, order_number, delivery_time, notes=None):
    """إنشاء رسالة تأكيد استلام الطلب بالتنسيق الموحد"""
    
    message = (
        f"✅ *تم قبول الطلب*\n\n"
        f"🔢 *رقم الطلب:* `{order_number}`\n"
        f"🆔 *معرف الطلب:* `{order_id}`\n\n"
        f"⏱️ *وقت التوصيل المتوقع:* {delivery_time} دقيقة\n"
    )
    
    if notes:
        message += f"\n📋 *ملاحظات:* {notes}"
    
    return message

def create_order_rejected_message(order_id, order_number, reason=None):
    """إنشاء رسالة رفض الطلب بالتنسيق الموحد"""
    
    message = (
        f"🚫 *تم رفض الطلب*\n\n"
        f"🔢 *رقم الطلب:* `{order_number}`\n"
        f"🆔 *معرف الطلب:* `{order_id}`\n\n"
    )
    
    if reason:
        message += f"📋 *سبب الرفض:* {reason}\n"
    else:
        message += f"📋 *سبب الرفض:* قد تكون معلومات المستخدم غير مكتملة أو غير واضحة.\n"
    
    message += "\nيمكنك اختيار *تعديل معلوماتي* لتصحيحها أو المحاولة لاحقاً."
    
    return message

def create_rating_response_message(order_id, order_number, rating, comment=None):
    """إنشاء رسالة تقييم للكاشير بالتنسيق الموحد"""
    
    stars = "⭐" * rating
    
    message = (
        f"📊 *تقييم جديد*\n\n"
        f"🔢 *رقم الطلب:* `{order_number}`\n"
        f"🆔 *معرف الطلب:* `{order_id}`\n\n"
        f"⭐ *التقييم:* {stars} ({rating}/5)\n"
    )
    
    if comment:
        message += f"💬 *التعليق:* {comment}\n"
    
    return message


# دوال استخراج المعلومات من الرسائل
def extract_order_id(text):
    """استخراج معرف الطلب من النص"""
    # محاولة استخراج معرف الطلب بالتنسيق الجديد
    match = re.search(r"🆔 \*معرف الطلب:\* `([^`]+)`", text)
    if match:
        return match.group(1)
    
    # محاولة استخراج معرف الطلب بالتنسيق القديم
    match = re.search(r"معرف الطلب:\s*[`\"']?([\w\d]+)[`\"']?", text)
    if match:
        return match.group(1)
    
    return None

def extract_order_number(text):
    """استخراج رقم الطلب من النص"""
    # محاولة استخراج رقم الطلب بالتنسيق الجديد
    match = re.search(r"🔢 \*رقم الطلب:\* `(\d+)`", text)
    if match:
        return int(match.group(1))
    
    # محاولة استخراج رقم الطلب بالتنسيق القديم
    match = re.search(r"رقم الطلب:?\s*[`\"']?(\d+)[`\"']?", text)
    if match:
        return int(match.group(1))
    
    # محاولة استخراج رقم الطلب من نص آخر
    match = re.search(r"طلبه رقم (\d+)", text)
    if match:
        return int(match.group(1))
    
    return None

def extract_rating(text):
    """استخراج التقييم من النص"""
    # محاولة استخراج التقييم بالتنسيق الجديد
    match = re.search(r"⭐ \*التقييم:\* (⭐+) \((\d+)/5\)", text)
    if match:
        return int(match.group(2))
    
    # محاولة استخراج التقييم بالتنسيق القديم
    match = re.search(r"تقييمه بـ (\⭐+)", text)
    if match:
        return len(match.group(1))
    
    return 0

def extract_comment(text):
    """استخراج التعليق من النص"""
    # محاولة استخراج التعليق بالتنسيق الجديد
    match = re.search(r"💬 \*التعليق:\* (.+?)(?:\n|$)", text)
    if match:
        return match.group(1).strip()
    
    # محاولة استخراج التعليق بالتنسيق القديم
    match = re.search(r"💬 التعليق: (.+?)(?:\n|$)", text)
    if match:
        return match.group(1).strip()
    
    return None




# ✅ دالة تحليل النجوم (يمكن حذفها لاحقًا إن لم تُستخدم)
def extract_stars(text: str) -> str:
    match = re.search(r"تقييمه بـ (\⭐+)", text)
    return match.group(1) if match else "⭐️"











main_menu_keyboard = ReplyKeyboardMarkup(
    [
        ["🚚 الدليفري"],
        ["📊 عدد الطلبات اليوم والدخل", "📅 عدد الطلبات أمس والدخل"],
        ["🗓️ طلبات الشهر الحالي", "📆 طلبات الشهر الماضي"],
        ["📈 طلبات السنة الحالية", "📉 طلبات السنة الماضية"],
        ["📋 إجمالي الطلبات والدخل"]
    ],
    resize_keyboard=True
)



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # مثال: استخرج restaurant_id من ملف config أو السياق (حسب البنية عندك)
    restaurant_id = RESTAURANT_ID  # تأكد أنه تم تخزينه مسبقًا

    if not restaurant_id:
        await update.message.reply_text("⚠️ معرف المطعم غير معروف. أعد تشغيل البوت.")
        return

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT COUNT(*) FROM delivery_persons WHERE restaurant_id = %s", (restaurant_id,)
                )
                result = await cursor.fetchone()
                delivery_count = result[0] if result else 0

        if delivery_count == 0:
            await update.message.reply_text(
                "🚫 لا يمكنك استقبال الطلبات حاليًا.\n"
                "يجب عليك إضافة دليفري واحد على الأقل لتفعيل الخدمة.\n\n"
                "➕ أضف دليفري الآن من خلال /start ثم اختر ➕ إضافة دليفري.",
                reply_markup=ReplyKeyboardMarkup([["➕ إضافة دليفري"]], resize_keyboard=True)
            )
            return

        await update.message.reply_text(
            "✅ بوت المطعم جاهز لاستقبال الطلبات !",
            reply_markup=main_menu_keyboard
        )

    except Exception as e:
        logger.error(f"❌ خطأ في دالة start: {e}")
        await update.message.reply_text("⚠️ حدث خطأ أثناء التحقق من الدليفري.")


# ✅ استقبال طلب من القناة
async def handle_channel_order(update: Update, context: CallbackContext):
    message = update.channel_post

    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""

    # تجاهل رسائل التقييم أو الإلغاء
    if "استلم طلبه رقم" in text and "قام بتقييمه بـ" in text:
        logger.info("ℹ️ تم تجاهل رسالة التقييم، ليست طلبًا جديدًا.")
        return

    if text.startswith("🚫 تم إلغاء الطلب رقم") or "تم إلغاء الطلب" in text:
        logger.info("⛔️ تم تجاهل رسالة إلغاء الطلب (ليست طلبًا جديدًا).")
        return

    logger.info(f"📥 استلم البوت طلبًا جديدًا من القناة: {text}")

    # استخراج المعرف ورقم الطلب
    order_id = extract_order_id(text)
    order_number = extract_order_number(text)

    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في الرسالة!")
        return

    logger.info(f"🔍 تم استخراج معرف الطلب: {order_id} | رقم الطلب: {order_number or 'غير معروف'}")

        global last_order_time
        async with order_rate_lock:
            now = time.time()
            elapsed = now - last_order_time
            if elapsed < 0.2:
                wait_time = 0.2 - elapsed
                logger.debug(f"⏳ انتظار {wait_time:.3f} ثانية لحماية الترتيب.")
                await asyncio.sleep(wait_time)
            last_order_time = time.time()


    # 🔐 الحصول على قفل تزامن خاص بهذا الطلب
    lock = await get_order_lock(order_id)

    # 🔒 منع التداخل عند معالجة الطلب نفسه
    async with lock:
        location = pending_locations.pop("last_location", None)
        message_text = text + ("\n\n📍 *تم إرفاق الموقع الجغرافي*" if location else "")

        keyboard = [
            [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")],
            [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{order_id}")],
            [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{order_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            # 1. بناء النص
            text_to_send = f"🆕 *طلب جديد من القناة:*\n\n{message_text}\n\n📌 *معرف الطلب:* `{order_id}`"
    
            # 2. إنشاء معرف تتبع
            message_id = str(uuid.uuid4())
    
            # 3. تتبع الرسالة
            await track_sent_message(
                message_id=message_id,
                order_id=order_id,
                source="restaurant_bot",
                destination="cashier",
                content=text_to_send
            )
    
            # 4. إرسال الموقع أولًا (إن وُجد)
            if location:
                latitude, longitude = location
                await context.bot.send_location(
                    chat_id=CASHIER_CHAT_ID,
                    latitude=latitude,
                    longitude=longitude
                )
                logger.info(f"✅ تم إرسال الموقع للكاشير (order_id={order_id})")
    
            # 5. إرسال الرسالة بعد الموقع
            sent_message = await send_message_with_retry(
                bot=context.bot,
                chat_id=CASHIER_CHAT_ID,
                text=text_to_send,
                order_id=order_id,
                message_id=message_id,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    
            logger.info(f"✅ تم إرسال الطلب إلى الكاشير (order_id={order_id})")
    
            # 6. حفظ الطلب مؤقتًا
            pending_orders[order_id] = {
                "order_details": message_text,
                "channel_message_id": message.message_id,
                "message_id": sent_message.message_id
            }
    
            # 7. حفظ الطلب في قاعدة البيانات
            await save_pending_order(order_id, message_text, message.message_id, sent_message.message_id, location)
    
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال الطلب إلى الكاشير: {e}")
       

async def handle_channel_location(update: Update, context: CallbackContext):
    global last_location_time
    async with location_rate_lock:
        now = time.time()
        elapsed = now - last_location_time
        if elapsed < 0.2:
            wait_time = 0.2 - elapsed
            logger.debug(f"⏳ انتظار {wait_time:.3f} ثانية قبل معالجة الموقع.")
            await asyncio.sleep(wait_time)
        last_location_time = time.time()
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if not message.location:
        return

    latitude = message.location.latitude
    longitude = message.location.longitude
    logger.info(f"📍 تم استلام موقع: {latitude}, {longitude}")

    # حفظ الموقع مؤقتًا في القاموس
    pending_locations["last_location"] = (latitude, longitude)

    # الحصول على آخر طلب غير مُعالَج
    last_order_id = max(pending_orders.keys(), default=None)
    if not last_order_id:
        logger.warning("⚠️ لا يوجد طلبات حالية لربط الموقع بها.")
        return

    # إضافة الموقع إلى الطلب
    pending_orders[last_order_id]["location"] = (latitude, longitude)
    updated_order_text = f"{pending_orders[last_order_id]['order_details']}\n\n📍 *تم إرفاق الموقع الجغرافي*"

    # أزرار التفاعل
    keyboard = [
        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{last_order_id}")],
        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{last_order_id}")],
        [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{last_order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # 1. إرسال الموقع الجغرافي
        await context.bot.send_location(
            chat_id=CASHIER_CHAT_ID,
            latitude=latitude,
            longitude=longitude
        )

        # 2. تحضير نص الرسالة
        text = f"🆕 *طلب جديد محدث من القناة:*\n\n{updated_order_text}\n\n📌 معرف الطلب: `{last_order_id}`"

        # 3. توليد UUID لتتبع الرسالة
        message_id = str(uuid.uuid4())

        # 4. تسجيل الرسالة في جدول التتبع
        await track_sent_message(
            message_id=message_id,
            order_id=last_order_id,
            source="restaurant_bot",
            destination="cashier",
            content=text
        )

        # 5. إرسال الرسالة مع retry
        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=text,
            order_id=last_order_id,
            message_id=message_id,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

        logger.info(f"✅ تم إرسال الطلب المحدث مع الموقع (order_id={last_order_id})")

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إرسال الطلب المحدث: {e}")


async def button(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    logger.info(f"📩 تم الضغط على زر: {data}")

    await query.answer()

    try:
        parts = data.split("_")
        if len(parts) < 2:
            logger.warning("⚠️ البيانات غير صالحة داخل callback_data.")
            return

        action = parts[0]
        if action == "report":
            report_type = f"{parts[0]}_{parts[1]}"
            order_id = "_".join(parts[2:])
        else:
            report_type = None
            order_id = "_".join(parts[1:])

        logger.debug(f"🔍 تم تحليل callback_data: action={action}, order_id={order_id}, report_type={report_type}")

        if order_id not in pending_orders:
            logger.warning(f"⚠️ الطلب غير موجود ضمن pending_orders: {order_id}")
            await query.answer("⚠️ هذا الطلب لم يعد متاحًا.", show_alert=True)
            return

        lock = await get_order_lock(order_id)
        async with lock:
            order_info = pending_orders[order_id]
            message_id = order_info.get("message_id")
            order_details = order_info.get("order_details", "")
            logger.debug(f"🔐 جاري تنفيذ الإجراء على الطلب: {order_id}")

            if action == "accept":
                logger.info("✅ تم اختيار 'قبول الطلب'، يتم عرض أزرار الوقت.")
                keyboard = [
                    [InlineKeyboardButton(f"{t} دقيقة", callback_data=f"time_{t}_{order_id}")]
                    for t in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90]
                ]
                keyboard.append([InlineKeyboardButton("📌 أكثر من 90 دقيقة", callback_data=f"time_90+_{order_id}")])
                keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")])
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                return

            elif action == "reject":
                logger.info("❌ تم اختيار 'رفض الطلب'، عرض أزرار التأكيد.")
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⚠️ تأكيد الرفض", callback_data=f"confirmreject_{order_id}")],
                        [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")]
                    ])
                )

            elif action == "confirmreject":
                logger.info("❌ تأكيد رفض الطلب، يتم إرسال إشعار إلى القناة.")
                await query.edit_message_reply_markup(reply_markup=None)
                reject_message = create_order_rejected_message(
                    order_id=order_id,
                    order_number=extract_order_number(order_details),
                    reason="قد تكون معلومات المستخدم غير مكتملة أو غير واضحة."
                )
                message_id_out = str(uuid.uuid4())
                await track_sent_message(message_id_out, order_id, "restaurant_bot", "channel", reject_message)
                await send_message_with_retry(
                    bot=context.bot,
                    chat_id=CHANNEL_ID,
                    text=reject_message,
                    order_id=order_id,
                    message_id=message_id_out,
                    parse_mode="Markdown"
                )
                logger.info(f"📢 تم إرسال إشعار رفض الطلب: {order_id}")
                

            elif action == "back":
                logger.info("🔙 تم الضغط على زر الرجوع، عرض الأزرار الرئيسية.")
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")],
                        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{order_id}")],
                        [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{order_id}")]
                    ])
                )

            elif action == "complain":
                logger.info("🚨 تم فتح قائمة الشكاوى.")
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚪 وصل الديليفري ولم يجد الزبون", callback_data=f"report_delivery_{order_id}")],
                        [InlineKeyboardButton("📞 رقم الهاتف غير صحيح", callback_data=f"report_phone_{order_id}")],
                        [InlineKeyboardButton("📍 معلومات الموقع غير دقيقة", callback_data=f"report_location_{order_id}")],
                        [InlineKeyboardButton("❓ مشكلة أخرى", callback_data=f"report_other_{order_id}")],
                        [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")]
                    ])
                )

            elif report_type:
                reason_map = {
                    "report_delivery": "🚪 وصل الديليفري ولم يجد الزبون",
                    "report_phone": "📞 رقم الهاتف غير صحيح",
                    "report_location": "📍 معلومات الموقع غير دقيقة",
                    "report_other": "❓ شكوى أخرى من الكاشير"
                }
                reason_text = reason_map.get(report_type, "شكوى غير معروفة")
                logger.info(f"📣 إرسال شكوى من النوع: {reason_text}")

                complaint_text = (
                    f"📣 *شكوى من الكاشير على الطلب:*\n"
                    f"📌 معرف الطلب: `{order_id}`\n"
                    f"📍 السبب: {reason_text}\n\n"
                    f"📝 *تفاصيل الطلب:*\n\n{order_details}"
                )

                message_id_1 = str(uuid.uuid4())
                await track_sent_message(message_id_1, order_id, "restaurant_bot", "complaints_channel", complaint_text)
                await send_message_with_retry(
                    context.bot,
                    chat_id=RESTAURANT_COMPLAINTS_CHAT_ID,
                    text=complaint_text,
                    order_id=order_id,
                    message_id=message_id_1,
                    parse_mode="Markdown"
                )

                cancel_text = (
                    f"🚫 تم إلغاء الطلب بسبب شكوى الكاشير.\n"
                    f"📌 معرف الطلب: `{order_id}`\n"
                    f"📍 السبب: {reason_text}"
                )
                message_id_2 = str(uuid.uuid4())
                await track_sent_message(message_id_2, order_id, "restaurant_bot", "channel", cancel_text)
                await send_message_with_retry(
                    context.bot,
                    chat_id=CHANNEL_ID,
                    text=cancel_text,
                    order_id=order_id,
                    message_id=message_id_2,
                    parse_mode="Markdown"
                )

                await query.edit_message_reply_markup(reply_markup=None)

                confirmation_text = "📨 تم إرسال الشكوى وإلغاء الطلب. سيتواصل معكم فريق الدعم إذا لزم الأمر."
                message_id_3 = str(uuid.uuid4())
                await track_sent_message(message_id_3, order_id, "restaurant_bot", "cashier", confirmation_text)
                await send_message_with_retry(
                    context.bot,
                    chat_id=CASHIER_CHAT_ID,
                    text=confirmation_text,
                    order_id=order_id,
                    message_id=message_id_3
                )

                logger.info(f"✅ تم إرسال الشكوى ومعالجة الطلب بالكامل: {order_id}")
                
    except Exception as e:
        logger.exception(f"❌ استثناء غير متوقع في button handler: {e}")



async def handle_time_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    match = re.match(r"time_(\d+\+?)_(.+)", query.data)
    if not match:
        return

    time_selected, order_id = match.groups()

    if order_id not in pending_orders:
        await query.answer("⚠️ الطلب غير متاح حالياً.", show_alert=True)
        return

    lock = await get_order_lock(order_id)

    async with lock:
        order_info = pending_orders[order_id]
        message_id = order_info.get("message_id")
        order_details = order_info.get("order_details", "")
        order_number = extract_order_number(order_details)

        try:
            # إزالة الأزرار
            await query.edit_message_reply_markup(reply_markup=None)

            # إرسال إشعار القبول للمستخدم
            accept_message = create_order_accepted_message(order_id, order_number, time_selected)

            message_id_channel = str(uuid.uuid4())
            await track_sent_message(message_id_channel, order_id, "restaurant_bot", "channel", accept_message)
            await send_message_with_retry(
                bot=context.bot,
                chat_id=CHANNEL_ID,
                text=accept_message,
                order_id=order_id,
                message_id=message_id_channel,
                parse_mode="Markdown"
            )

            # إرسال تأكيد القبول للكاشير
            confirm_text = f"✅ تم قبول الطلب وإبلاغ المستخدم بوقت التوصيل: {time_selected} دقيقة."
            message_id_cashier = str(uuid.uuid4())
            await track_sent_message(message_id_cashier, order_id, "restaurant_bot", "cashier", confirm_text)
            await send_message_with_retry(
                bot=context.bot,
                chat_id=CASHIER_CHAT_ID,
                text=confirm_text,
                order_id=order_id,
                message_id=message_id_cashier
            )

            logger.info(f"✅ تم قبول الطلب وإبلاغ المستخدم. (order_id={order_id}, time={time_selected})")

        except Exception as e:
            logger.error(f"❌ فشل في إرسال إشعار القبول: {e}")

       








# 🔔 إعادة إرسال التذكير كما هو
async def handle_channel_reminder(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    if "تذكير من الزبون" not in text:
        return

    logger.info(f"📥 استلم البوت تذكيرًا جديدًا: {text}")

    # استخراج معرف الطلب
    order_id = extract_order_id(text)
    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في التذكير.")
        return

    try:
        # 1. تجهيز النص
        reminder_text = f"🔔 *تذكير من الزبون!*\n\n{text}"

        # 2. توليد message_id وتتبع الرسالة
        message_id = str(uuid.uuid4())
        await track_sent_message(
            message_id=message_id,
            order_id=order_id,
            source="restaurant_bot",
            destination="cashier",
            content=reminder_text
        )

        # 3. الإرسال مع ربط message_id
        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=reminder_text,
            order_id=order_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

        logger.info("📩 تم إرسال التذكير إلى الكاشير بنجاح!")

    except Exception as e:
        logger.error(f"⚠️ خطأ أثناء إرسال التذكير إلى الكاشير: {e}")


# 🔔 إعادة إرسال التذكير بصيغة أخرى
async def handle_reminder_message(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if "تذكير من الزبون" in message.text:
        logger.info("📌 تم استلام تذكير من الزبون، إعادة توجيهه للكاشير...")
        try:
            # 1. النص المراد إرساله
            text = message.text
        
            # 2. توليد message_id فريد وتسجيل الرسالة
            message_id = str(uuid.uuid4())
            await track_sent_message(
                message_id=message_id,
                order_id=order_id,  # تأكد أن order_id معرف مسبقًا في نفس الدالة
                source="restaurant_bot",
                destination="cashier",
                content=text
            )
        
            # 3. الإرسال مع ربط message_id بالتتبع
            await send_message_with_retry(
                bot=context.bot,
                chat_id=CASHIER_CHAT_ID,
                text=text,
                order_id=order_id,
                message_id=message_id
            )
        
            logger.info("✅ تم إرسال التذكير إلى الكاشير بنجاح.")
        
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال التذكير للكاشير: {e}")



# ⏳ استفسار "كم يتبقى؟"
async def handle_time_left_question(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    if "كم يتبقى" not in text or "الطلب رقم" not in text:
        return

    logger.info("📥 تم استلام استفسار عن المدة المتبقية للطلب...")

    order_number = extract_order_number(text)
    if not order_number:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب في الاستفسار.")
        return

    try:
        await context.bot.send_message(
            chat_id=CASHIER_CHAT_ID,
            text=(
                f"⏳ الزبون عم يسأل كم باقي لطلبه رقم {order_number}؟\n"
                f"🔁 ارجع لرسالة الطلب واختر الوقت من الأزرار المرفقة تحتها 🙏"
            )
        )
        logger.info(f"✅ تم إرسال إشعار استفسار المدة للكاشير للطلب رقم {order_number}.")

    except Exception as e:
        logger.error(f"❌ فشل في إرسال إشعار الوقت للكاشير: {e}")


# ⭐ استلام التقييم من الزبون
async def handle_rating_feedback(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 استلمنا إشعار تقييم من الزبون: {text}")

    match = re.search(r"رقم (\d+)", text)
    if not match:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب في إشعار التقييم!")
        return

    order_number = match.group(1)

    for order_id, data in pending_orders.items():
        if f"رقم الطلب:* `{order_number}`" in data["order_details"]:
            message_id = data.get("message_id")
            if not message_id:
                logger.warning(f"⚠️ لا يوجد message_id محفوظ للطلب: {order_id}")
                return
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=CASHIER_CHAT_ID,
                    message_id=message_id,
                    reply_markup=None
                )
                logger.info(f"✅ تم إزالة الأزرار من رسالة الطلب رقم: {order_number}")
            except Exception as e:
                logger.error(f"❌ فشل في إزالة الأزرار: {e}")
           



# ✅ استلام التقييم من الزبون
async def handle_order_delivered_rating(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 محتوى رسالة القناة (لتقييم الطلب): {text}")

    if "استلم طلبه رقم" not in text or "معرف الطلب" not in text:
        logger.info("ℹ️ تم تجاهل رسالة التقييم، ليست كاملة.")
        return

    order_number_match = re.search(r"طلبه رقم\s*(\d+)", text)
    order_number = order_number_match.group(1) if order_number_match else None

    order_id_match = re.search(r"معرف الطلب:\s*(\w+)", text)
    order_id = order_id_match.group(1) if order_id_match else None

    if not order_number or not order_id:
        logger.warning("⚠️ لم يتم استخراج رقم الطلب أو معرف الطلب من رسالة التقييم.")
        return

    logger.info(f"🔍 تم استلام تقييم لطلب رقم: {order_number} - معرف الطلب: {order_id}")

    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ لم يتم العثور على الطلب بمعرف: {order_id}")
        return

    message_id = order_data.get("message_id")
    if not message_id:
        logger.warning(f"⚠️ لا يوجد message_id محفوظ للطلب: {order_id}")
        return

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=CASHIER_CHAT_ID,
            message_id=message_id,
            reply_markup=None
        )
        logger.info(f"✅ تم إزالة أزرار الطلب رقم {order_number} (معرف: {order_id})")

        stars = extract_stars(text)

        # 1. إعداد النص
        message_text = f"✅ الزبون استلم طلبه رقم {order_number} وقام بتقييمه بـ {stars}"
        
        # 2. توليد معرف فريد وتتبع الرسالة
        message_id = str(uuid.uuid4())
        await track_sent_message(
            message_id=message_id,
            order_id=order_id,  # تأكد من توفر order_id أو استخرجه من النص
            source="restaurant_bot",
            destination="cashier",
            content=message_text
        )
        
        # 3. إرسال الرسالة مع retry وتحديد message_id
        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=message_text,
            order_id=order_id,
            message_id=message_id
        )


    except Exception as e:
        logger.error(f"❌ خطأ أثناء إزالة الأزرار أو إرسال إشعار: {e}")
    


async def handle_report_cancellation_notice(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 تم استلام إشعار إلغاء مع تقرير: {text}")

    # 🧠 استخراج رقم ومعرف الطلب
    order_id = extract_order_id(text)
    order_number = extract_order_number(text)

    if not order_id or not order_number:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب أو معرف الطلب في الرسالة.")
        return

    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ الطلب غير موجود في pending_orders: {order_id}")
        return

    cashier_message_id = order_data.get("message_id")
    if not cashier_message_id:
        logger.warning(f"⚠️ لا يوجد message_id محفوظ للطلب: {order_id}")
        return

    # 🔍 استخراج سبب الإلغاء الحقيقي من نص الرسالة
    import re
    reason_match = re.search(r"💬 سبب الإلغاء:\n(.+)", text, re.DOTALL)
    reason = reason_match.group(1).strip() if reason_match else "لم يُذكر سبب واضح."

    try:
        # 1. حذف الأزرار
        await context.bot.edit_message_reply_markup(
            chat_id=CASHIER_CHAT_ID,
            message_id=cashier_message_id,
            reply_markup=None
        )
        logger.info(f"✅ تم إزالة أزرار الطلب رقم {order_number} (معرف: {order_id})")

        # 2. تجهيز نص الرسالة
        message_text = (
            f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
            f"📌 معرف الطلب: `{order_id}`\n"
            f"📍 السبب المذكور:\n{reason}\n\n"
            f"📞 يمكنكم التواصل مع الزبون عبر رقم الهاتف المرفق في الطلب."
        )

        # 3. تتبع الرسالة
        message_id = str(uuid.uuid4())
        await track_sent_message(
            message_id=message_id,
            order_id=order_id,
            source="restaurant_bot",
            destination="cashier",
            content=message_text
        )

        # 4. إرسال إلى الكاشير
        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=message_text,
            order_id=order_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة إلغاء مع تقرير: {e}")

   


# ✅ استلام إلغاء الطلب من الزبون (إلغاء عادي أو بسبب التأخر)
async def handle_standard_cancellation_notice(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 تم استلام إشعار إلغاء: {text}")

    # 🧠 استخراج رقم ومعرف الطلب باستخدام الدوال الموحدة
    order_id = extract_order_id(text)
    order_number = extract_order_number(text)

    if not order_id or not order_number:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب أو معرف الطلب في الرسالة.")
        return

    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ الطلب غير موجود في pending_orders: {order_id}")
        return

    cashier_message_id = order_data.get("message_id")
    if not cashier_message_id:
        logger.warning(f"⚠️ لا يوجد message_id محفوظ للطلب: {order_id}")
        return

    try:
        # 🧼 إزالة الأزرار من رسالة الكاشير
        await context.bot.edit_message_reply_markup(
            chat_id=CASHIER_CHAT_ID,
            message_id=cashier_message_id,
            reply_markup=None
        )
        logger.info(f"✅ تم إزالة أزرار الطلب رقم {order_number} (معرف: {order_id})")

        # 📨 إعداد رسالة الإلغاء
        message_text = (
            f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
            f"📌 معرف الطلب: `{order_id}`\n"
            f"📍 السبب: تأخر بالموافقة أو تردد الزبون.\n\n"
            f"يرجى الانتباه في المرات القادمة.\n"
            f"نحن سنعتذر منه وندعوه للطلب لاحقًا بسبب ضغط الطلبات."
        )

        # 🧠 تتبع الرسالة
        message_id = str(uuid.uuid4())
        await track_sent_message(
            message_id=message_id,
            order_id=order_id,
            source="restaurant_bot",
            destination="cashier",
            content=message_text
        )

        # 🚀 إرسال الرسالة
        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=message_text,
            order_id=order_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إرسال إشعار إلغاء الطلب: {e}")

    




async def handle_rating_message(update: Update, context: CallbackContext):
    message = update.channel_post

    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""

    if "قام بتقييمه بـ" not in text:
        return

    order_id = extract_order_id(text)
    order_number = extract_order_number(text)
    rating = extract_rating(text)
    comment = extract_comment(text)

    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في رسالة التقييم!")
        return

    cashier_message = create_rating_response_message(
        order_id=order_id,
        order_number=order_number,
        rating=rating,
        comment=comment
    )

    try:
        message_id = str(uuid.uuid4())

        await track_sent_message(
            message_id=message_id,
            order_id=order_id,
            source="restaurant_bot",
            destination="cashier",
            content=cashier_message
        )

        await send_message_with_retry(
            bot=context.bot,
            chat_id=CASHIER_CHAT_ID,
            text=cashier_message,
            order_id=order_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

        logger.info(f"✅ تم إرسال التقييم إلى الكاشير (order_id={order_id})")

    except Exception as e:
        logger.error(f"❌ فشل في إرسال التقييم إلى الكاشير: {e}")








async def handle_delivery_menu(update: Update, context: CallbackContext):
    context.user_data["delivery_action"] = "menu"
    reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
    await update.message.reply_text(
        "📦 إدارة الدليفري:\nاختر الإجراء المطلوب:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
    )


async def ask_add_delivery_name(update: Update, context: CallbackContext):
    context.user_data["delivery_action"] = "adding_name"
    await update.message.reply_text(
        "🧑‍💼 ما اسم الدليفري؟",
        reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True)
    )


async def handle_add_delivery(update: Update, context: CallbackContext):
    text = update.message.text.strip()

    if text == "🔙 رجوع":
        context.user_data.clear()
        await start(update, context)
        return

    action = context.user_data.get("delivery_action")

    if action == "adding_name":
        context.user_data["new_delivery_name"] = text
        context.user_data["delivery_action"] = "adding_phone"
        await update.message.reply_text(
            "📞 ما رقم الهاتف؟",
            reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True)
        )

    elif action == "adding_phone":
        name = context.user_data.get("new_delivery_name")
        phone = text
        restaurant_id = RESTAURANT_ID

        if not restaurant_id:
            await update.message.reply_text("⚠️ معرف المطعم غير معروف. أعد تشغيل البوت.")
            return

        try:
            async with get_db_connection() as db:
                async with db.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO delivery_persons (restaurant_id, name, phone) VALUES (%s, %s, %s)",
                        (restaurant_id, name, phone)
                    )
                await db.commit()

            context.user_data.pop("delivery_action", None)
            context.user_data.pop("new_delivery_name", None)

            reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
            await update.message.reply_text(
                f"✅ تم إضافة الدليفري:\n🧑‍💼 {name}\n📞 {phone}",
                reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
            )

        except Exception as e:
            logger.error(f"❌ خطأ أثناء إضافة الدليفري: {e}")
            await update.message.reply_text("⚠️ حدث خطأ أثناء حفظ الدليفري. حاول مرة أخرى.")


async def handle_delete_delivery_menu(update: Update, context: CallbackContext):
    restaurant_id = RESTAURANT_ID

    try:
        async with get_db_connection() as db:
            async with db.cursor() as cursor:
                await cursor.execute(
                    "SELECT name FROM delivery_persons WHERE restaurant_id = %s",
                    (restaurant_id,)
                )
                rows = await cursor.fetchall()

        if not rows:
            await update.message.reply_text(
                "⚠️ لا يوجد أي دليفري مسجل حالياً.",
                reply_markup=ReplyKeyboardMarkup([["➕ إضافة دليفري"], ["🔙 رجوع"]], resize_keyboard=True)
            )
            return

        if len(rows) == 1:
            await update.message.reply_text(
                "🚫 لا يمكنك حذف آخر دليفري.\nأضف بديلاً له أولاً قبل الحذف.",
                reply_markup=ReplyKeyboardMarkup([["➕ إضافة دليفري"], ["🔙 رجوع"]], resize_keyboard=True)
            )
            return

        names = [row[0] for row in rows]
        context.user_data["delivery_action"] = "deleting"
        await update.message.reply_text(
            "🗑 اختر اسم الدليفري الذي تريد حذفه:",
            reply_markup=ReplyKeyboardMarkup([[name] for name in names] + [["🔙 رجوع"]], resize_keyboard=True)
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب قائمة الدليفري للحذف: {e}")
        await update.message.reply_text("⚠️ حدث خطأ أثناء عرض القائمة.")


async def handle_delete_delivery_choice(update: Update, context: CallbackContext):
    text = update.message.text.strip()

    if text == "🔙 رجوع":
        context.user_data.clear()
        await start(update, context)
        return

    if context.user_data.get("delivery_action") != "deleting":
        return

    restaurant_id = RESTAURANT_ID

    try:
        async with get_db_connection() as db:
            async with db.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM delivery_persons WHERE restaurant_id = %s AND name = %s",
                    (restaurant_id, text)
                )
            await db.commit()

        context.user_data.pop("delivery_action", None)

        reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
        await update.message.reply_text(
            f"✅ تم حذف الدليفري: {text}",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء حذف الدليفري: {e}")
        await update.message.reply_text("⚠️ حدث خطأ أثناء حذف الدليفري.")


async def unified_delivery_router(update: Update, context: CallbackContext):
    action = context.user_data.get("delivery_action")

    if action in ["adding_name", "adding_phone"]:
        await handle_add_delivery(update, context)
    elif action == "deleting":
        await handle_delete_delivery_choice(update, context)
    else:
        await update.message.reply_text("❓ يرجى اختيار خيار من القائمة أولاً.")



async def handle_yesterday_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).date()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price)
                    FROM orders
                    WHERE DATE(timestamp) = %s
                """, (yesterday,))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📅 *إحصائيات يوم أمس:*\n\n"
            f"🔢 عدد الطلبات: {count}\n"
            f"💰 الدخل الكلي: {total} ل.س",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ فشل استخراج إحصائيات أمس: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء استخراج الإحصائيات.")



async def handle_today_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price) 
                    FROM orders 
                    WHERE DATE(timestamp) = %s
                """, (today,))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📊 *إحصائيات اليوم*\n\n"
            f"🔢 عدد الطلبات: *{count}*\n"
            f"💰 الدخل الكلي: *{total}* ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ فشل استخراج إحصائيات اليوم: {e}")


async def handle_current_month_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.datetime.now()
    first_day = today.replace(day=1).date().isoformat()
    last_day = today.date().isoformat()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price)
                    FROM orders
                    WHERE DATE(timestamp) BETWEEN %s AND %s
                """, (first_day, last_day))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"🗓️ *إحصائيات الشهر الحالي:*\n\n"
            f"🔢 عدد الطلبات: {count}\n"
            f"💰 الدخل الكلي: {total} ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء استخراج إحصائيات الشهر الحالي: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء استخراج البيانات.")


async def handle_last_month_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.datetime.now()
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - datetime.timedelta(days=1)
    first_day_last_month = last_day_last_month.replace(day=1)

    start_date = first_day_last_month.date().isoformat()
    end_date = last_day_last_month.date().isoformat()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price)
                    FROM orders
                    WHERE DATE(timestamp) BETWEEN %s AND %s
                """, (start_date, end_date))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📆 *إحصائيات الشهر الماضي:*\n\n"
            f"🔢 عدد الطلبات: {count}\n"
            f"💰 الدخل الكلي: {total} ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء استخراج إحصائيات الشهر الماضي: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء استخراج البيانات.")



async def handle_current_year_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now()
    start_date = today.replace(month=1, day=1).date().isoformat()
    end_date = today.date().isoformat()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price)
                    FROM orders
                    WHERE DATE(timestamp) BETWEEN %s AND %s
                """, (start_date, end_date))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📈 *إحصائيات السنة الحالية:*\n\n"
            f"🔢 عدد الطلبات: {count}\n"
            f"💰 الدخل الكلي: {total} ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء استخراج إحصائيات السنة الحالية: {e}")


async def handle_last_year_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now()
    last_year = today.year - 1
    start_date = f"{last_year}-01-01"
    end_date = f"{last_year}-12-31"

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT COUNT(*), SUM(total_price)
                    FROM orders
                    WHERE DATE(timestamp) BETWEEN %s AND %s
                """, (start_date, end_date))
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📉 *إحصائيات السنة الماضية ({last_year}):*\n\n"
            f"🔢 عدد الطلبات: {count}\n"
            f"💰 الدخل الكلي: {total} ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء استخراج إحصائيات السنة الماضية: {e}")


async def handle_total_stats(update: Update, context: CallbackContext):
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT COUNT(*), SUM(total_price) FROM orders")
                result = await cursor.fetchone()

        count = result[0] or 0
        total = result[1] or 0

        await update.message.reply_text(
            f"📋 *إجمالي الإحصائيات:*\n\n"
            f"🔢 عدد كل الطلبات: {count}\n"
            f"💰 مجموع الدخل: {total} ل.س",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء استخراج إجمالي الإحصائيات: {e}")


async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error(msg="🚨 حدث استثناء أثناء معالجة التفاعل:", exc_info=context.error)

    traceback_str = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    print("⚠️ تفاصيل الخطأ:\n", traceback_str)

    try:
        if update and hasattr(update, 'callback_query') and update.callback_query.message:
            await update.callback_query.message.reply_text("❌ حدث خطأ غير متوقع أثناء تنفيذ العملية. سيتم التحقيق في الأمر.")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء محاولة إرسال إشعار الخطأ: {e}")



# ✅ **إعداد البوت وتشغيله**
# ✅ دالة معالجة أخطاء الشبكة
async def handle_network_error(update, context):
    logger.error(f"🌐 حدث خطأ في الشبكة: {context.error}")
    if isinstance(context.error, NetworkError):
        await asyncio.sleep(1)
        if hasattr(update, 'message') and update.message:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ حدث خلل مؤقت بالشبكة. سيتم إعادة المحاولة تلقائيًا."
                )
            except:
                pass


ORDER_ID_PATTERNS = [
    r"معرف الطلب:?\s*[`\"']?([\w\d]+)[`\"']?",
    r"🆔.*?[`\"']?([\w\d]+)[`\"']?",
    r"order_id:?\s*[`\"']?([\w\d]+)[`\"']?"
]

ORDER_NUMBER_PATTERNS = [
    r"رقم الطلب:?\s*[`\"']?(\d+)[`\"']?",
    r"🔢.*?[`\"']?(\d+)[`\"']?",
    r"order_number:?\s*[`\"']?(\d+)[`\"']?"
]

def extract_order_id(text):
    for pattern in ORDER_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def extract_order_number(text):
    for pattern in ORDER_NUMBER_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None



# ✅ إعداد وتشغيل البوت
async def run_bot():
    global app

    # 🧠 إعداد جلسة HTTP مخصصة لتحسين الأداء والثبات
    request = HTTPXRequest(
        connection_pool_size=100,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )

    # ✅ بناء التطبيق مع إعدادات الاتصال والمعالجة المتزامنة
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()
    # ✅ أوامر البوت
    app.add_handler(CommandHandler("start", start))

    app.add_error_handler(handle_network_error)

    # ✅ إشعار تقييم الطلب من الزبون
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r"^✅ الزبون استلم طلبه رقم \d+ وقام بتقييمه بـ .+?\n📌 معرف الطلب: "),
        handle_order_delivered_rating
    ))

    # ✅ إشعارات الإلغاء
    app.add_handler(MessageHandler(
    filters.ChatType.CHANNEL & filters.Regex(r"🚫 تم إلغاء الطلب رقم \d+ من قبل الزبون.*📌 معرف الطلب:"),
    handle_standard_cancellation_notice
))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex("تأخر المطعم.*تم إنشاء تقرير"), handle_report_cancellation_notice))

    # ✅ رسائل التذكير
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"تذكير من الزبون"), handle_channel_reminder))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"كم يتبقى.*الطلب رقم"), handle_time_left_question))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.LOCATION, handle_channel_location))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, handle_channel_order))

    # ✅ أزرار التفاعل
    app.add_handler(CallbackQueryHandler(button, pattern=r"^(accept|reject|confirmreject|back|complain|report_(delivery|phone|location|other))_.+"))
    app.add_handler(CallbackQueryHandler(handle_time_selection, pattern=r"^time_\d+\+?_.+"))

   # ✅ إدارة الدليفري
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🚚 الدليفري"), handle_delivery_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("➕ إضافة دليفري"), ask_add_delivery_name))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("❌ حذف دليفري"), handle_delete_delivery_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_delivery_router))
 


    # ✅ أوامر الإحصائيات
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📊 عدد الطلبات اليوم والدخل"), handle_today_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📅 عدد الطلبات أمس والدخل"), handle_yesterday_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🗓️ طلبات الشهر الحالي"), handle_current_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📆 طلبات الشهر الماضي"), handle_last_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📈 طلبات السنة الحالية"), handle_current_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📉 طلبات السنة الماضية"), handle_last_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📋 إجمالي الطلبات والدخل"), handle_total_stats))

    # ✅ تقييمات من القناة
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT & filters.Regex("قام بتقييمه بـ"), handle_rating_message))

    # ✅ معالجة الأخطاء
    app.add_error_handler(error_handler)
    asyncio.create_task(start_order_queue_processor())


    # ✅ تشغيل البوت
    await app.run_polling()


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()

    
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logging.info("🚀 جارٍ بدء تشغيل بوت المطعم (النسخة المعدلة للبيئات ذات الـ loop النشط).")

    try:
        loop = asyncio.get_event_loop()
        logging.info("📌 جدولة دالة run_bot على الـ event loop الموجود.")
        task = loop.create_task(run_bot())

        def _log_task_exception_if_any(task_future):
            if task_future.done() and task_future.exception():
                logging.error("❌ مهمة run_bot انتهت بخطأ:", exc_info=task_future.exception())

        task.add_done_callback(_log_task_exception_if_any)

        loop.run_forever()  # ⬅️ هذه تبقي البوت نشطًا للأبد

    except KeyboardInterrupt:
        logging.info("🛑 تم إيقاف السكربت يدويًا (KeyboardInterrupt).")
    except Exception as e:
        logging.error(f"❌ حدث خطأ فادح في التنفيذ الرئيسي: {e}", exc_info=True)
