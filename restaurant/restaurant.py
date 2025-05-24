import sys
import json 
import logging
import re
import os
import traceback
import datetime
import aiomysql
import pymysql
import asyncio
import nest_asyncio
from telegram.error import TelegramError
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, CallbackContext
from contextlib import asynccontextmanager
from telegram.ext import ContextTypes
from telegram.request import HTTPXRequest
from collections import deque



# قفل التزامن للطلبات
order_locks = {}

async def get_order_lock(order_id):
    """الحصول على قفل خاص بطلب معين"""
    if order_id not in order_locks:
        order_locks[order_id] = asyncio.Lock()
    return order_locks[order_id]





# محدد معدل الطلبات
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    async def acquire(self):
        now = time.time()

        # إزالة الطلبات القديمة
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()

        # إذا وصلنا للحد الأقصى، انتظر
        if len(self.calls) >= self.max_calls:
            wait_time = self.calls[0] + self.period - now
            await asyncio.sleep(wait_time)

        # تسجيل الطلب الجديد
        self.calls.append(time.time())

# إنشاء محدد معدل للطلبات
telegram_limiter = RateLimiter(max_calls=30, period=1)  # 30 طلب في الثانية

# دالة لإرسال رسالة مع إعادة المحاولة
async def send_message_with_retry(bot, chat_id, text, order_id=None, max_retries=5, **kwargs):
    message_id = str(uuid.uuid4())  # إنشاء معرف فريد للرسالة
    
    # محاولة إرسال الرسالة مع إعادة المحاولة
    for attempt in range(max_retries):
        try:
            # تطبيق محدد معدل الطلبات
            await telegram_limiter.acquire()
            
            # إرسال الرسالة
            sent_message = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            
            # إرجاع الرسالة المرسلة
            return sent_message
            
        except Exception as e:
            logger.error(f"فشل في إرسال الرسالة (المحاولة {attempt+1}/{max_retries}): {e}")
            
            # انتظار قبل إعادة المحاولة (زيادة وقت الانتظار مع كل محاولة)
            wait_time = 0.5 * (2 ** attempt)  # 0.5, 1, 2, 4, 8 ثواني
            await asyncio.sleep(wait_time)
    
    # رفع استثناء بعد فشل جميع المحاولات
    raise Exception(f"فشلت جميع المحاولات ({max_retries}) لإرسال الرسالة.")





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
        # إنشاء الاتصال بقاعدة البيانات
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
                restaurant VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                phone VARCHAR(20) NOT NULL
            )
        """)

        conn.commit()
        conn.close()

        logger.info("✅ تم التأكد من وجود جدول الطلبات وجدول الدليفري.")

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إنشاء الجداول: {e}")


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

# ✅ دالة تحليل النجوم (يمكن حذفها لاحقًا إن لم تُستخدم)
def extract_stars(text: str) -> str:
    match = re.search(r"تقييمه بـ (\⭐+)", text)
    return match.group(1) if match else "⭐️"


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
    try:
        await update.message.reply_text(
            "✅ بوت المطعم جاهز لاستقبال الطلبات !",
            reply_markup=main_menu_keyboard
        )
    except Exception as e:
        logger.error(f"❌ خطأ في دالة start: {e}")


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

    # استخراج المعرف ورقم الطلب بالتنسيق الموحد أو القديم
    order_id = extract_order_id(text)
    order_number = extract_order_number(text)

    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في الرسالة!")
        return

    logger.info(f"🔍 تم استخراج معرف الطلب: {order_id} | رقم الطلب: {order_number or 'غير معروف'}")

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
            sent_message = await send_message_with_retry(
                context.bot,
                chat_id=CASHIER_CHAT_ID,
                text=f"🆕 *طلب جديد من القناة:*\n\n{message_text}\n\n📌 *معرف الطلب:* `{order_id}`",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            logger.info(f"✅ تم إرسال الطلب إلى الكاشير (order_id={order_id})")

            pending_orders[order_id] = {
                "order_details": message_text,
                "channel_message_id": message.message_id,
                "message_id": sent_message.message_id
            }

            if location:
                try:
                    latitude, longitude = location
                    await context.bot.send_location(
                        chat_id=CASHIER_CHAT_ID,
                        latitude=latitude,
                        longitude=longitude
                    )
                    logger.info(f"✅ تم إرسال الموقع للكاشير (order_id={order_id})")
                except Exception as e:
                    logger.error(f"❌ فشل إرسال الموقع: {e}")

        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال الطلب إلى الكاشير: {e}")



async def handle_channel_location(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if not message.location:
        return

    latitude = message.location.latitude
    longitude = message.location.longitude
    logger.info(f"📍 تم استلام موقع: {latitude}, {longitude}")
    pending_locations["last_location"] = (latitude, longitude)

    last_order_id = max(pending_orders.keys(), default=None)
    if not last_order_id:
        logger.warning("⚠️ لا يوجد طلبات حالية لربط الموقع بها.")
        return

    pending_orders[last_order_id]["location"] = (latitude, longitude)
    updated_order_text = f"{pending_orders[last_order_id]['order_details']}\n\n📍 *تم إرفاق الموقع الجغرافي*"

    keyboard = [
        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{last_order_id}")],
        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{last_order_id}")],
        [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{last_order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_location(chat_id=CASHIER_CHAT_ID, latitude=latitude, longitude=longitude)
        await send_message_with_retry(context.bot, 
            chat_id=CASHIER_CHAT_ID,
            text=f"🆕 *طلب جديد محدث من القناة:*\n\n{updated_order_text}\n\n📌 معرف الطلب: `{last_order_id}`",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        logger.info(f"✅ تم إرسال الطلب المحدث مع الموقع (order_id={last_order_id})")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء إرسال الطلب المحدث: {e}")





async def button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    if len(data) < 2:
        return

    action = data[0]

    if action == "report":
        report_type = f"{data[0]}_{data[1]}"
        order_id = "_".join(data[2:])
    else:
        report_type = None
        order_id = "_".join(data[1:])

    if order_id not in pending_orders:
        await query.answer("⚠️ هذا الطلب لم يعد متاحًا.", show_alert=True)
        return

    # 🔐 الحصول على قفل خاص بهذا الطلب
    lock = await get_order_lock(order_id)

    # 🔒 منع التداخل عند معالجة نفس الطلب
    async with lock:
        order_info = pending_orders[order_id]
        message_id = order_info.get("message_id")
        order_details = order_info.get("order_details", "")

        if action == "accept":
            keyboard = [
                [InlineKeyboardButton(f"{t} دقيقة", callback_data=f"time_{t}_{order_id}")]
                for t in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90]
            ]
            keyboard.append([InlineKeyboardButton("📌 أكثر من 90 دقيقة", callback_data=f"time_90+_{order_id}")])
            keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")])

            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            except TelegramError as e:
                logger.error(f"❌ فشل في تعديل الأزرار (accept): {e}")
            return

        elif action == "confirmreject":
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=CASHIER_CHAT_ID,
                    message_id=message_id,
                    reply_markup=None
                )

                reject_message = create_order_rejected_message(
                    order_id=order_id,
                    order_number=extract_order_number(order_details),
                    reason="قد تكون معلومات المستخدم غير مكتملة أو غير واضحة."
                )

                await send_message_with_retry(
                    context.bot,
                    chat_id=CHANNEL_ID,
                    text=reject_message,
                    parse_mode="Markdown"
                )

                logger.info(f"✅ تم رفض الطلب وإبلاغ المستخدم. (order_id={order_id})")
            except TelegramError as e:
                logger.error(f"❌ فشل في إرسال إشعار رفض الطلب: {e}")
            finally:
                pending_orders.pop(order_id, None)

        elif action == "back":
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")],
                        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{order_id}")],
                        [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{order_id}")]
                    ])
                )
            except TelegramError as e:
                logger.error(f"❌ فشل في عرض أزرار الرجوع: {e}")

        elif action == "complain":
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚪 وصل الديليفري ولم يجد الزبون", callback_data=f"report_delivery_{order_id}")],
                        [InlineKeyboardButton("📞 رقم الهاتف غير صحيح", callback_data=f"report_phone_{order_id}")],
                        [InlineKeyboardButton("📍 معلومات الموقع غير دقيقة", callback_data=f"report_location_{order_id}")],
                        [InlineKeyboardButton("❓ مشكلة أخرى", callback_data=f"report_other_{order_id}")],
                        [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")]
                    ])
                )
            except TelegramError as e:
                logger.error(f"❌ فشل في عرض أزرار الشكاوى: {e}")

        elif report_type:
            reason_map = {
                "report_delivery": "🚪 وصل الديليفري ولم يجد الزبون",
                "report_phone": "📞 رقم الهاتف غير صحيح",
                "report_location": "📍 معلومات الموقع غير دقيقة",
                "report_other": "❓ شكوى أخرى من الكاشير"
            }

            reason_text = reason_map.get(report_type, "شكوى غير معروفة")

            try:
                await send_message_with_retry(
                    context.bot,
                    chat_id=RESTAURANT_COMPLAINTS_CHAT_ID,
                    text=(
                        f"📣 *شكوى من الكاشير على الطلب:*\n"
                        f"📌 معرف الطلب: `{order_id}`\n"
                        f"📍 السبب: {reason_text}\n\n"
                        f"📝 *تفاصيل الطلب:*\n\n{order_details}"
                    ),
                    parse_mode="Markdown"
                )

                await send_message_with_retry(
                    context.bot,
                    chat_id=CHANNEL_ID,
                    text=(
                        f"🚫 تم إلغاء الطلب بسبب شكوى الكاشير.\n"
                        f"📌 معرف الطلب: `{order_id}`\n"
                        f"📍 السبب: {reason_text}"
                    ),
                    parse_mode="Markdown"
                )

                await context.bot.edit_message_reply_markup(
                    chat_id=CASHIER_CHAT_ID,
                    message_id=message_id,
                    reply_markup=None
                )

                await send_message_with_retry(
                    context.bot,
                    chat_id=CASHIER_CHAT_ID,
                    text="📨 تم إرسال الشكوى وإلغاء الطلب. سيتواصل معكم فريق الدعم إذا لزم الأمر."
                )

                logger.info(f"✅ تم إرسال شكوى بنجاح وتم تنظيف الطلب: {order_id}")

            except TelegramError as e:
                logger.error(f"❌ خطأ أثناء إرسال الشكوى: {e}")
            finally:
                pending_orders.pop(order_id, None)



async def handle_time_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    _, time_selected, order_id = query.data.split("_")

    # 🔐 الحصول على قفل خاص بهذا الطلب
    lock = await get_order_lock(order_id)

    # 🔒 استخدام القفل لمنع التداخل
    async with lock:
        # تحديث الأزرار
        keyboard = [
            [InlineKeyboardButton(f"✅ {t} دقيقة" if str(t) == time_selected else f"{t} دقيقة", callback_data=f"time_{t}_{order_id}")]
            for t in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90]
        ]
        keyboard.append([InlineKeyboardButton("🚗 جاهز ليطلع", callback_data=f"ready_{order_id}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"⚠️ لم يتم تحديث الأزرار: {e}")

        order_data = pending_orders.get(order_id)
        if not order_data:
            logger.warning(f"⚠️ الطلب غير موجود في pending_orders: {order_id}")
            return

        order_details = order_data["order_details"]

        # بناء الرسالة الموحدة للمستخدم
        accept_message = create_order_accepted_message(
            order_id=order_id,
            order_number=extract_order_number(order_details),
            delivery_time=time_selected
        )

        try:
            await send_message_with_retry(
                context.bot,
                chat_id=CHANNEL_ID,
                text=accept_message,
                parse_mode="Markdown"
            )

            await send_message_with_retry(
                context.bot,
                chat_id=CASHIER_CHAT_ID,
                text=f"✅ تم قبول الطلب وإبلاغ المستخدم بوقت التوصيل: {time_selected} دقيقة."
            )

            logger.info(f"✅ تم قبول الطلب وإبلاغ المستخدم. (order_id={order_id}, time={time_selected})")

        except Exception as e:
            logger.error(f"❌ فشل في إرسال إشعار القبول: {e}")
        finally:
            pending_orders.pop(order_id, None)





# 🔔 إعادة إرسال التذكير كما هو
async def handle_channel_reminder(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if "تذكير من الزبون" in message.text:
        logger.info(f"📥 استلم البوت تذكيرًا جديدًا: {message.text}")
        try:
            await send_message_with_retry(context.bot, 
                chat_id=CASHIER_CHAT_ID,
                text=f"🔔 *تذكير من الزبون!*\n\n{message.text}",
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
            await send_message_with_retry(context.bot, 
                chat_id=CASHIER_CHAT_ID,
                text=message.text
            )
            logger.info("✅ تم إرسال التذكير إلى الكاشير بنجاح.")
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال التذكير للكاشير: {e}")


# ⏳ استفسار "كم يتبقى؟"
async def handle_time_left_question(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if "كم يتبقى" in message.text and "الطلب رقم" in message.text:
        logger.info("📥 تم استلام استفسار عن المدة المتبقية للطلب...")
        try:
            await send_message_with_retry(context.bot, 
                chat_id=CASHIER_CHAT_ID,
                text=f"⏳ *استفسار من الزبون:*\n\n{message.text}",
                parse_mode="Markdown"
            )
            logger.info("✅ تم إرسال الاستفسار إلى الكاشير بنجاح.")
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال الاستفسار للكاشير: {e}")



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
            finally:
                pending_orders.pop(order_id, None)  # 🧹 تنظيف الطلب بعد التقييم
            break




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

        await send_message_with_retry(context.bot, 
            chat_id=CASHIER_CHAT_ID,
            text=f"✅ الزبون استلم طلبه رقم {order_number} وقام بتقييمه بـ {stars}"
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إزالة الأزرار أو إرسال إشعار: {e}")
    finally:
        pending_orders.pop(order_id, None)


# ✅ استلام إلغاء الطلب من الزبون
async def handle_report_cancellation_notice(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 تم استلام إشعار إلغاء مع تقرير: {text}")

    order_number_match = re.search(r"إلغاء الطلب رقم[:\s]*(\d+)", text)
    order_number = order_number_match.group(1) if order_number_match else None

    order_id_match = re.search(r"معرف الطلب[:\s]*`?([\w\d]+)`?", text)
    order_id = order_id_match.group(1) if order_id_match else None

    if not order_number or not order_id:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب أو معرف الطلب في رسالة الإلغاء.")
        return

    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ الطلب غير موجود في pending_orders: {order_id}")
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

        await send_message_with_retry(context.bot, 
            chat_id=CASHIER_CHAT_ID,
            text=(
                f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"📍 السبب: تأخر المطعم وتم إنشاء تقرير بالمشكلة وسنتواصل مع الزبون ومعكم لنفهم سبب الإلغاء.\n\n"
                f"📞 يمكنكم التواصل مع الزبون عبر رقم الهاتف المرفق في الطلب."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة إلغاء مع تقرير: {e}")
    finally:
        pending_orders.pop(order_id, None)


# ✅ استلام إلغاء الطلب من الزبون
async def handle_standard_cancellation_notice(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 تم استلام إشعار إلغاء عادي: {text}")

    order_number_match = re.search(r"إلغاء الطلب رقم[:\s]*(\d+)", text)
    order_number = order_number_match.group(1) if order_number_match else None

    order_id_match = re.search(r"معرف الطلب[:\s]*`?([\w\d]+)`?", text)
    order_id = order_id_match.group(1) if order_id_match else None

    if not order_number or not order_id:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب أو معرف الطلب في رسالة الإلغاء.")
        return

    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ الطلب غير موجود في pending_orders: {order_id}")
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

        await send_message_with_retry(context.bot, 
            chat_id=CASHIER_CHAT_ID,
            text=(
                f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"📍 السبب: تردد الزبون وقرر الإلغاء."
            ),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة الإلغاء العادي: {e}")
    finally:
        pending_orders.pop(order_id, None)


async def handle_delivery_menu(update: Update, context: CallbackContext):
    reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
    await update.message.reply_text(
        "📦 إدارة :\nاختر الإجراء المطلوب:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
    )
    context.user_data["delivery_action"] = "menu"

async def handle_add_delivery(update: Update, context: CallbackContext):
    text = update.message.text

    # 🔙 الرجوع من أي خطوة
    if text == "🔙 رجوع":
        context.user_data.pop("delivery_action", None)
        context.user_data.pop("new_delivery_name", None)
        reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
        await update.message.reply_text("⬅️ تم الرجوع إلى قائمة الدليفري.", reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True))
        return

    action = context.user_data.get("delivery_action")

    # 🧑‍💼 المرحلة 1: استلام الاسم
    if action == "adding_name":
        context.user_data["new_delivery_name"] = text
        context.user_data["delivery_action"] = "adding_phone"
        await update.message.reply_text("📞 ما رقم الهاتف؟", reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True))

    # ☎️ المرحلة 2: استلام الرقم وحفظ البيانات
    elif action == "adding_phone":
        name = context.user_data.get("new_delivery_name")
        phone = text
        restaurant_name = context.user_data.get("restaurant")  # تأكد أنه مخزن مسبقًا

        try:
            async with await get_db_connection() as db:
                await db.execute(
                    "INSERT INTO delivery_persons (restaurant, name, phone) VALUES (?, ?, ?)",
                    (restaurant_name, name, phone)
                )
                await db.commit()

            # ✅ إنهاء العملية
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

async def ask_add_delivery_name(update: Update, context: CallbackContext):
    context.user_data["delivery_action"] = "adding_name"
    await update.message.reply_text("🧑‍💼 ما اسم الدليفري؟", reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True))

async def handle_delete_delivery_menu(update: Update, context: CallbackContext):
    restaurant_name = context.user_data.get("restaurant")

    try:
        async with await get_db_connection() as db:
            async with db.execute(
                "SELECT name FROM delivery_persons WHERE restaurant = ?", (restaurant_name,)
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await update.message.reply_text("⚠️ لا يوجد أي دليفري مسجل حالياً.", reply_markup=ReplyKeyboardMarkup(
                [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]], resize_keyboard=True
            ))
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
    text = update.message.text

    # الرجوع
    if text == "🔙 رجوع":
        context.user_data.pop("delivery_action", None)
        reply_keyboard = [["➕ إضافة دليفري", "❌ حذف دليفري"], ["🔙 رجوع"]]
        await update.message.reply_text("⬅️ تم الرجوع إلى قائمة الدليفري.", reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True))
        return

    if context.user_data.get("delivery_action") != "deleting":
        return  # تجاهل

    restaurant_name = context.user_data.get("restaurant")

    try:
        async with await get_db_connection() as db:
            await db.execute(
                "DELETE FROM delivery_persons WHERE restaurant = ? AND name = ?",
                (restaurant_name, text)
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





async def handle_rating_message(update: Update, context: CallbackContext):
    message = update.channel_post
    
    if not message or message.chat_id != CHANNEL_ID:
        return
    
    text = message.text or ""
    
    # التحقق من أن الرسالة هي رسالة تقييم
    if "قام بتقييمه بـ" not in text:
        return
    
    # استخراج معلومات التقييم
    order_id = extract_order_id(text)
    order_number = extract_order_number(text)
    rating = extract_rating(text)
    comment = extract_comment(text)
    
    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في رسالة التقييم!")
        return
    
    # إنشاء رسالة التقييم للكاشير
    cashier_message = create_rating_response_message(
        order_id=order_id,
        order_number=order_number,
        rating=rating,
        comment=comment
    )
    
    # إرسال التقييم إلى الكاشير
    try:
        await send_message_with_retry(context.bot, 
            chat_id=CASHIER_CHAT_ID,
            text=cashier_message,
            parse_mode="Markdown"
        )
        logger.info(f"✅ تم إرسال التقييم إلى الكاشير (order_id={order_id})")
    except Exception as e:
        logger.error(f"❌ فشل في إرسال التقييم إلى الكاشير: {e}")





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
async def run_bot():
    global app
    request = HTTPXRequest()  # ← جلسة منفصلة لكل بوت
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    # ✅ إنشاء قاعدة البيانات
    await initialize_database()

    # ✅ أوامر البوت
    app.add_handler(CommandHandler("start", start))

    # ✅ إشعار تقييم الطلب من الزبون
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r"^✅ الزبون استلم طلبه رقم \d+ وقام بتقييمه بـ .+?\n📌 معرف الطلب: "), 
        handle_order_delivered_rating
    ))

    # ✅ معالجة الأخطاء
    app.add_error_handler(error_handler)

    # ✅ إشعارات الإلغاء
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex("تردد الزبون"), handle_standard_cancellation_notice))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex("تأخر المطعم.*تم إنشاء تقرير"), handle_report_cancellation_notice))

    # ✅ رسائل التذكير
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"تذكير من الزبون"), handle_channel_reminder))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"كم يتبقى.*الطلب رقم"), handle_time_left_question))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.LOCATION, handle_channel_location))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, handle_channel_order))

    # ✅ أزرار التفاعل
    app.add_handler(CallbackQueryHandler(button, pattern=r"^(accept|reject|confirmreject|back|complain|report_(delivery|phone|location|other))_.+"))
    app.add_handler(CallbackQueryHandler(handle_time_selection, pattern=r"^time_\d+_.+"))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🚚 الدليفري"), handle_delivery_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("➕ إضافة دليفري"), ask_add_delivery_name))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_delivery))  
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("❌ حذف دليفري"), handle_delete_delivery_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete_delivery_choice))

    # ✅ أوامر الإحصائيات
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📊 عدد الطلبات اليوم والدخل"), handle_today_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📅 عدد الطلبات أمس والدخل"), handle_yesterday_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🗓️ طلبات الشهر الحالي"), handle_current_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📆 طلبات الشهر الماضي"), handle_last_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📈 طلبات السنة الحالية"), handle_current_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📉 طلبات السنة الماضية"), handle_last_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📋 إجمالي الطلبات والدخل"), handle_total_stats))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT & filters.Regex("قام بتقييمه بـ"), handle_rating_message))

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
