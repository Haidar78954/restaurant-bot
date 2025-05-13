import logging
import re
import sqlite3
import os
import datetime
from telegram import ReplyKeyboardMarkup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, CallbackContext


# ✅ إنشاء ملف قاعدة البيانات إذا لم يكن موجودًا
DB_PATH = "restaurant_orders.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# ✅ إنشاء جدول الطلبات إذا لم يكن موجودًا
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    order_number INTEGER,
    restaurant TEXT,
    total_price INTEGER,
    timestamp TEXT
)
""")
conn.commit()

# 🔹 إعداد سجل الأخطاء لمتابعة المشاكل
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔹 توكن بوت المطعم
TOKEN = '7114672578:AAEz5UZMD2igBFRhJrs9Rb1YCS4fkku-Jjc'  # استبدله بالتوكن الصحيح

# 🔹 معرف الكاشير - **تأكد من وضع المعرف الصحيح**
CASHIER_CHAT_ID = 5065182020  # استبدل هذا بمعرف الكاشير الحقيقي

# 🔹 معرف القناة التي سيتم استقبال الطلبات منها
CHANNEL_ID = -1002471456650  # استبدل هذا بمعرف القناة الصحيح

RESTAURANT_COMPLAINTS_CHAT_ID = -4791648333  # ✅ عرّفها هنا فقط مرة واحدة

# 🔹 قاموس لتخزين الطلبات المفتوحة باستخدام معرف الطلب
pending_orders = {}
pending_locations = {}
def extract_stars(text: str) -> str:
    match = re.search(r"تقييمه بـ (\⭐+)", text)
    return match.group(1) if match else "⭐️"


# ✅ **التأكد من أن البوت يعمل**
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "✅ بوت المطعم جاهز لاستقبال الطلبات من القناة!",
        reply_markup=get_admin_main_menu()
    )



async def handle_channel_order(update: Update, context: CallbackContext):
    message = update.channel_post

    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""

    if "استلم طلبه رقم" in text and "قام بتقييمه بـ" in text:
        logger.info("ℹ️ تم تجاهل رسالة التقييم، ليست طلبًا جديدًا.")
        return

    if text.startswith("🚫 تم إلغاء الطلب رقم"):
        logger.info("⛔️ تم تجاهل رسالة إلغاء الطلب (ليست طلبًا جديدًا).")
        return

    logger.info(f"📥 استلم البوت طلبًا جديدًا من القناة: {text}")

    match = re.search(r"معرف الطلب:\s*([\w\d]+)", text)
    if not match:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في الرسالة!")
        return

    order_id = match.group(1)
    logger.info(f"🔍 تم استخراج معرف الطلب: {order_id}")

    location = pending_locations.pop("last_location", None)

    message_text = text
    if location:
        message_text += "\n\n📍 *تم إرفاق الموقع الجغرافي*"

    # ✅ الأزرار الجديدة
    keyboard = [
        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")],
        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        sent_message = await context.bot.send_message(
            chat_id=CASHIER_CHAT_ID,
            text=f"🆕 *طلب جديد من القناة:*\n\n{message_text}\n\n📌 معرف الطلب: `{order_id}`",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

        logger.info(f"✅ تم إرسال الطلب إلى الكاشير بنجاح! (معرف الطلب: {order_id})")

        pending_orders[order_id] = {
            "order_details": message_text,
            "channel_message_id": message.message_id,
            "message_id": sent_message.message_id
        }

        if location:
            latitude, longitude = location
            await context.bot.send_location(chat_id=CASHIER_CHAT_ID, latitude=latitude, longitude=longitude)
            logger.info(f"✅ تم إرسال بطاقة الموقع إلى الكاشير للطلب: {order_id}")

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إرسال الطلب إلى الكاشير: {e}")


async def handle_channel_location(update: Update, context: CallbackContext):
    message = update.channel_post

    if not message or message.chat_id != CHANNEL_ID:
        return

    if message.location:
        latitude = message.location.latitude
        longitude = message.location.longitude

        logger.info(f"📍 استلمنا موقعًا: {latitude}, {longitude} وسيتم حفظه مؤقتًا.")
        pending_locations["last_location"] = (latitude, longitude)

        last_order_id = max(pending_orders.keys(), default=None)
        if last_order_id:
            pending_orders[last_order_id]["location"] = (latitude, longitude)
            logger.info(f"📌 تم ربط الموقع بالطلب: {last_order_id}")

            updated_order_text = f"{pending_orders[last_order_id]['order_details']}\n\n📍 *تم إرفاق الموقع الجغرافي*"

            keyboard = [
                [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{last_order_id}")],
                [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{last_order_id}")],
                [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{last_order_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await context.bot.send_location(
                    chat_id=CASHIER_CHAT_ID,
                    latitude=latitude,
                    longitude=longitude
                )
                logger.info(f"✅ تم إرسال بطاقة الموقع إلى الكاشير للطلب: {last_order_id}")

                await context.bot.send_message(
                    chat_id=CASHIER_CHAT_ID,
                    text=f"🆕 *طلب جديد محدث من القناة:*\n\n{updated_order_text}\n\n📌 معرف الطلب: `{last_order_id}`",
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
                logger.info(f"✅ تم إرسال الطلب المحدث مع الموقع إلى الكاشير: {last_order_id}")

            except Exception as e:
                logger.error(f"❌ خطأ أثناء إرسال الطلب المحدث إلى الكاشير: {e}")







# ✅ **التعامل مع تفاعل الكاشير مع الطلبات (قبول / رفض)**
async def button(update: Update, context: CallbackContext):
    """📌 معالجة تفاعل الكاشير مع الطلبات (قبول / رفض / تحديد وقت التحضير / شكاوى)"""
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    if len(data) < 2:
        return

    action = data[0]

    # ✅ إذا كانت شكوى report_xx_ يجب معالجة الاسم بدقة
    if action == "report":
        report_type = f"{data[0]}_{data[1]}"  # مثل report_phone
        order_id = "_".join(data[2:])
    else:
        report_type = None
        order_id = "_".join(data[1:])

    if order_id not in pending_orders:
        await query.answer("⚠️ هذا الطلب لم يعد متاحًا.", show_alert=True)
        return

    order_info = pending_orders[order_id]
    message_id = order_info.get("message_id")
    order_details = order_info.get("order_details", "")

    # ✅ قبول الطلب: عرض أزرار الوقت + زر رجوع
    if action == "accept":
        keyboard = [
            [InlineKeyboardButton(f"{t} دقيقة", callback_data=f"time_{t}_{order_id}")]
            for t in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90]
        ]
        keyboard.append([InlineKeyboardButton("📌 أكثر من 90 دقيقة", callback_data=f"time_90+_{order_id}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ✅ رفض الطلب: عرض خيارات تأكيد / رجوع
    elif action == "reject":
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأكيد رفض الطلب", callback_data=f"confirmreject_{order_id}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")]
            ])
        )

    # ✅ تأكيد رفض الطلب
    elif action == "confirmreject":
        await context.bot.edit_message_reply_markup(chat_id=CASHIER_CHAT_ID, message_id=message_id, reply_markup=None)

        text = (
            f"🚫 تم رفض الطلب.\n"
            f"📌 معرف الطلب: `{order_id}`\n"
            "📍 السبب: قد تكون معلومات المستخدم غير مكتملة أو غير واضحة.\n"
            "يمكنك اختيار *تعديل معلوماتي* لتصحيحها.\n"
            "أو ربما منطقتك لا تغطيها خدمة التوصيل.\n"
            "جرب اختيار مطعم أقرب أو المحاولة لاحقًا إن كانت هناك مشكلة لدى المطعم."
        )
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"⚠️ خطأ أثناء إرسال إشعار الرفض: {e}")

    # ✅ الرجوع إلى الأزرار الأساسية
    elif action == "back":
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")],
                [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject_{order_id}")],
                [InlineKeyboardButton("🚨 شكوى عن الزبون أو الطلب", callback_data=f"complain_{order_id}")]
            ])
        )

    # ✅ فتح قائمة الشكاوى
    elif action == "complain":
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 وصل الديليفري ولم يجد الزبون", callback_data=f"report_delivery_{order_id}")],
                [InlineKeyboardButton("📞 رقم الهاتف غير صحيح", callback_data=f"report_phone_{order_id}")],
                [InlineKeyboardButton("📍 معلومات الموقع غير دقيقة", callback_data=f"report_location_{order_id}")],
                [InlineKeyboardButton("❓ مشكلة أخرى", callback_data=f"report_other_{order_id}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")]
            ])
        )

    # ✅ إرسال الشكوى إلى مجموعة التقارير + قناة المطعم
    elif report_type:
        reason_map = {
            "report_delivery": "🚪 وصل الديليفري ولم يجد الزبون",
            "report_phone": "📞 رقم الهاتف غير صحيح",
            "report_location": "📍 معلومات الموقع غير دقيقة",
            "report_other": "❓ شكوى أخرى من الكاشير"
        }

        reason_text = reason_map.get(report_type, "شكوى غير معروفة")

        try:
            # ✅ إرسال الشكوى مع تفاصيل الطلب إلى مجموعة التقارير
            await context.bot.send_message(
                chat_id=RESTAURANT_COMPLAINTS_CHAT_ID,
                text=(
                    f"📣 *شكوى من الكاشير على الطلب:*\n"
                    f"📌 معرف الطلب: `{order_id}`\n"
                    f"📍 السبب: {reason_text}\n\n"
                    f"📝 *تفاصيل الطلب:*\n\n{order_details}"
                ),
                parse_mode="Markdown"
            )

            # ✅ إرسال إشعار إلى القناة لإبلاغ المستخدم
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=(
                    f"🚫 تم إلغاء الطلب بسبب شكوى الكاشير.\n"
                    f"📌 معرف الطلب: `{order_id}`\n"
                    f"📍 السبب: {reason_text}"
                ),
                parse_mode="Markdown"
            )

            # ✅ إزالة الأزرار
            await context.bot.edit_message_reply_markup(chat_id=CASHIER_CHAT_ID, message_id=message_id, reply_markup=None)

            # ✅ إعلام الكاشير
            await context.bot.send_message(
                chat_id=CASHIER_CHAT_ID,
                text="📨 تم إرسال الشكوى وإلغاء الطلب. سيتواصل معكم فريق الدعم إذا لزم الأمر."
            )

            logger.info(f"📣 شكوى وإلغاء الطلب أُرسلت بنجاح. معرف الطلب: {order_id} - السبب: {reason_text}")

        except Exception as e:
            logger.error(f"⚠️ فشل إرسال الشكوى أو إشعار الإلغاء: {e}")







async def handle_time_selection(update: Update, context: CallbackContext):
    """⏳ معالجة اختيار الكاشير لوقت التحضير، وتسجيل الطلب في قاعدة البيانات"""

    query = update.callback_query
    await query.answer()

    # استخراج مدة التحضير ومعرف الطلب
    _, time_selected, order_id = query.data.split("_")

    # 🔹 تحديث الأزرار لتحديد الوقت المختار + زر الرجوع
    keyboard = [
        [InlineKeyboardButton(f"✅ {t} دقيقة" if str(t) == time_selected else f"{t} دقيقة", callback_data=f"time_{t}_{order_id}")]
        for t in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90]
    ]
    keyboard.append([InlineKeyboardButton("📌 أكثر من 90 دقيقة", callback_data=f"time_90+_{order_id}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"back_{order_id}")])  # ✅ زر الرجوع

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"⚠️ لم يتم تحديث الأزرار: {e}")

    # 🔍 استخراج بيانات الطلب من pending_orders
    order_data = pending_orders.get(order_id)
    if not order_data:
        logger.warning(f"⚠️ لم يتم العثور على الطلب في pending_orders: {order_id}")
        return

    order_text = order_data["order_details"]

    # ✅ استخراج رقم الطلب
    order_number_match = re.search(r"رقم الطلب[:\s]*([0-9]+)", order_text)
    order_number = int(order_number_match.group(1)) if order_number_match else 0

    # ✅ استخراج السعر من "المجموع الكلي"
    total_price_match = re.search(r"المجموع الكلي[:\s]*([0-9,]+)", order_text)
    if total_price_match:
        total_price_str = total_price_match.group(1).replace(",", "")
        total_price = int(total_price_str)
    else:
        total_price = 0

    # ✅ استخراج اسم المطعم إن وجد
    restaurant_match = re.search(r"المطعم[:\s]*(.+)", order_text)
    restaurant = restaurant_match.group(1).strip() if restaurant_match else "غير معروف"

    # ✅ تسجيل الطلب في قاعدة البيانات
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect("restaurant_orders.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO orders (order_id, order_number, restaurant, total_price, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (order_id, order_number, restaurant, total_price, timestamp))
        conn.commit()
        conn.close()
        logger.info(f"✅ تم حفظ الطلب في قاعدة البيانات: {order_id}")
    except Exception as e:
        logger.error(f"❌ فشل تسجيل الطلب في قاعدة البيانات: {e}")

    # ✅ إرسال إشعار إلى القناة
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=(
            f"🔥 *الطلب عالنار بالمطبخ!* 🍽️\n\n"
            f"📌 *معرف الطلب:* `{order_id}`\n"
            f"⏳ *مدة التحضير:* {time_selected} دقيقة"
        ),
        parse_mode="Markdown"
    )





async def handle_channel_reminder(update: Update, context: CallbackContext):
    """ 🔔 يلتقط بوت المطعم التذكير من القناة ويعيد إرساله إلى الكاشير كما هو """

    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    # ✅ تأكيد أن الرسالة فعلاً تذكير من الزبون
    if "تذكير من الزبون" in message.text:
        logger.info(f"📥 استلم البوت تذكيرًا جديدًا: {message.text}")

        try:
            await context.bot.send_message(
                chat_id=CASHIER_CHAT_ID,
                text=f"🔔 *تذكير من الزبون!*\n\n{message.text}",
                parse_mode="Markdown"
            )
            logger.info("📩 تم إرسال التذكير إلى الكاشير بنجاح!")

        except Exception as e:
            logger.error(f"⚠️ خطأ أثناء إرسال التذكير إلى الكاشير: {e}")



# 📌 التقاط رسائل التذكير من القناة (مثل: "تذكير من الزبون: الطلب رقم 12...")
async def handle_reminder_message(update: Update, context: CallbackContext):
    message = update.channel_post

    if not message or message.chat_id != CHANNEL_ID:
        return

    if "تذكير من الزبون" in message.text:
        logger.info("📌 تم استلام تذكير من الزبون، إعادة توجيهه للكاشير...")
        try:
            await context.bot.send_message(
                chat_id=CASHIER_CHAT_ID,
                text=message.text
            )
            logger.info("✅ تم إرسال التذكير إلى الكاشير بنجاح.")
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال التذكير للكاشير: {e}")


async def handle_time_left_question(update: Update, context: CallbackContext):
    """ ⏳ يلتقط بوت المطعم سؤال 'كم يتبقى؟' من القناة ويرسله للكاشير """
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    if "كم يتبقى" in message.text and "الطلب رقم" in message.text:
        logger.info("📥 تم استلام استفسار عن المدة المتبقية للطلب، جاري تحويله للكاشير...")

        try:
            await context.bot.send_message(
                chat_id=CASHIER_CHAT_ID,
                text=f"⏳ *استفسار من الزبون:*\n\n{message.text}",
                parse_mode="Markdown"
            )
            logger.info("✅ تم إرسال الاستفسار إلى الكاشير بنجاح.")
        except Exception as e:
            logger.error(f"❌ خطأ أثناء إرسال الاستفسار للكاشير: {e}")

async def handle_rating_feedback(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 استلمنا إشعار تقييم من الزبون: {text}")

    # ✅ استخراج رقم الطلب من النص
    match = re.search(r"رقم (\d+)", text)
    if not match:
        logger.warning("⚠️ لم يتم العثور على رقم الطلب في إشعار التقييم!")
        return

    order_number = match.group(1)

    # ✅ ابحث عن رسالة الطلب المرتبطة بهذا الرقم
    for order_id, data in pending_orders.items():
        if f"رقم الطلب:* `{order_number}`" in data["order_details"]:
            message_id = data.get("message_id")  # الرسالة الأصلية في محادثة الكاشير
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
            break

async def handle_order_delivered_rating(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 محتوى رسالة القناة (لتقييم الطلب): {text}")

    # ✅ التأكد من وجود النصين المطلوبين
    if "استلم طلبه رقم" not in text or "معرف الطلب" not in text:
        logger.info("ℹ️ تم تجاهل رسالة التقييم، ليست كاملة.")
        return

    # ✅ استخراج رقم الطلب
    order_number_match = re.search(r"طلبه رقم\s*(\d+)", text)
    order_number = order_number_match.group(1) if order_number_match else None

    # ✅ استخراج معرف الطلب
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
        # ✅ إزالة الأزرار من رسالة الكاشير
        await context.bot.edit_message_reply_markup(
            chat_id=CASHIER_CHAT_ID,
            message_id=message_id,
            reply_markup=None
        )
        logger.info(f"✅ تم إزالة أزرار الطلب رقم {order_number} (معرف: {order_id})")

        # ✅ استخراج التقييم (عدد النجوم)
        stars = extract_stars(text)

        # ✅ إرسال إشعار للكاشير بالتقييم
        await context.bot.send_message(
            chat_id=CASHIER_CHAT_ID,
            text=f"✅ الزبون استلم طلبه رقم {order_number} وقام بتقييمه بـ {stars}"
        )

        # ✅ حذف الطلب من الذاكرة
        del pending_orders[order_id]

    except Exception as e:
        logger.error(f"❌ خطأ أثناء إزالة الأزرار أو إرسال إشعار: {e}")


async def handle_standard_cancellation_notice(update: Update, context: CallbackContext):
    message = update.channel_post
    if not message or message.chat_id != CHANNEL_ID:
        return

    text = message.text or ""
    logger.info(f"📩 تم استلام إشعار إلغاء عادي: {text}")

    # ✅ استخراج رقم الطلب
    order_number_match = re.search(r"إلغاء الطلب رقم[:\s]*(\d+)", text)
    order_number = order_number_match.group(1) if order_number_match else None

    # ✅ استخراج معرف الطلب
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

        await context.bot.send_message(
            chat_id=CASHIER_CHAT_ID,
            text=(
                f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"📍 السبب: تردد الزبون وقرر الإلغاء."
            ),
            parse_mode="Markdown"
        )

        del pending_orders[order_id]

    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة الإلغاء العادي: {e}")




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

        await context.bot.send_message(
            chat_id=CASHIER_CHAT_ID,
            text=(
                f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"📍 السبب: تأخر المطعم وتم إنشاء تقرير بالمشكلة وسنتواصل مع الزبون ومعكم لنفهم سبب الإلغاء.\n\n"
                f"📞 يمكنكم التواصل مع الزبون عبر رقم الهاتف المرفق في الطلب."
            ),
            parse_mode="Markdown"
        )

        del pending_orders[order_id]

    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة إلغاء مع تقرير: {e}")


async def handle_today_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    # استخراج الطلبات من تاريخ اليوم الحالي بناءً على timestamp
    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) = ?
    """, (today,))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📊 *إحصائيات اليوم*\n\n"
        f"🔢 عدد الطلبات: *{count}*\n"
        f"💰 الدخل الكلي: *{total}* ل.س",
        parse_mode="Markdown"
    )


async def handle_yesterday_stats(update: Update, context: CallbackContext):
    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    # تحديد تاريخ البارحة بتنسيق تاريخ فقط
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).date()

    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) = ?
    """, (yesterday.isoformat(),))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📅 *إحصائيات يوم أمس:*\n\n"
        f"🔢 عدد الطلبات: {count}\n"
        f"💰 الدخل الكلي: {total} ل.س",
        parse_mode="Markdown"
    )


async def handle_current_month_stats(update: Update, context: CallbackContext):
    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    today = datetime.datetime.now()
    first_day = today.replace(day=1).date()
    last_day = today.date()

    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) BETWEEN ? AND ?
    """, (first_day.isoformat(), last_day.isoformat()))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"🗓️ *إحصائيات الشهر الحالي:*\n\n"
        f"🔢 عدد الطلبات: {count}\n"
        f"💰 الدخل الكلي: {total} ل.س",
        parse_mode="Markdown"
    )


async def handle_last_month_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now()
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - datetime.timedelta(days=1)
    first_day_last_month = last_day_last_month.replace(day=1)

    start_date = first_day_last_month.date().isoformat()
    end_date = last_day_last_month.date().isoformat()

    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) BETWEEN ? AND ?
    """, (start_date, end_date))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📆 *إحصائيات الشهر الماضي:*\n\n"
        f"🔢 عدد الطلبات: {count}\n"
        f"💰 الدخل الكلي: {total} ل.س",
        parse_mode="Markdown"
    )

async def handle_current_year_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now()
    start_date = today.replace(month=1, day=1).date().isoformat()
    end_date = today.date().isoformat()

    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) BETWEEN ? AND ?
    """, (start_date, end_date))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📈 *إحصائيات السنة الحالية:*\n\n"
        f"🔢 عدد الطلبات: {count}\n"
        f"💰 الدخل الكلي: {total} ل.س",
        parse_mode="Markdown"
    )


async def handle_last_year_stats(update: Update, context: CallbackContext):
    today = datetime.datetime.now()
    last_year = today.year - 1
    start_date = f"{last_year}-01-01"
    end_date = f"{last_year}-12-31"

    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), SUM(total_price) 
        FROM orders 
        WHERE DATE(timestamp) BETWEEN ? AND ?
    """, (start_date, end_date))

    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📉 *إحصائيات السنة الماضية ({last_year}):*\n\n"
        f"🔢 عدد الطلبات: {count}\n"
        f"💰 الدخل الكلي: {total} ل.س",
        parse_mode="Markdown"
    )

async def handle_total_stats(update: Update, context: CallbackContext):
    conn = sqlite3.connect("restaurant_orders.db")
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*), SUM(total_price) FROM orders")
    result = cursor.fetchone()
    conn.close()

    count = result[0] or 0
    total = result[1] or 0

    await update.message.reply_text(
        f"📋 *إجمالي الإحصائيات:*\n\n"
        f"🔢 عدد كل الطلبات: {count}\n"
        f"💰 مجموع الدخل: {total} ل.س",
        parse_mode="Markdown"
    )



import traceback

async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error(msg="🚨 حدث استثناء أثناء معالجة التفاعل:", exc_info=context.error)

    # سجل تفاصيل الخطأ أيضًا في ملف أو في اللوج
    traceback_str = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))

    if update and hasattr(update, 'callback_query'):
        await update.callback_query.message.reply_text("❌ حدث خطأ غير متوقع أثناء تنفيذ العملية. سيتم التحقيق في الأمر.")

    # طباعة التفاصيل في اللوج لمساعدتك في التتبع
    print("⚠️ تفاصيل الخطأ:\n", traceback_str)



# ✅ **إعداد البوت وتشغيله**
def main():
    app = Application.builder().token(TOKEN).build()

    # ✅ أوامر البوت
    app.add_handler(CommandHandler("start", start))

    # ✅ 1. إشعار تقييم الطلب من الزبون (يجب أن يكون أولاً)
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r"^✅ الزبون استلم طلبه رقم \d+ وقام بتقييمه بـ .+?\n📌 معرف الطلب: "), 
        handle_order_delivered_rating
    ))

    app.add_error_handler(error_handler)


    # إشعار إلغاء عادي
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex("تردد الزبون"),
        handle_standard_cancellation_notice
    ))

    # إشعار إلغاء مع تقرير
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex("تأخر المطعم.*تم إنشاء تقرير"),
        handle_report_cancellation_notice
    ))


    # ✅ 3. رسائل التذكير
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"تذكير من الزبون"), handle_channel_reminder))

    # ✅ 4. سؤال "كم يتبقى؟"
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.Regex(r"كم يتبقى.*الطلب رقم"), handle_time_left_question))

    # ✅ 5. الموقع الجغرافي من القناة
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.LOCATION, handle_channel_location))

    # ✅ 6. الطلبات النصية الجديدة (يجب أن تكون آخر شرط نصي عام)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, handle_channel_order))

    # ✅ أزرار التفاعل (قبول/رفض/الوقت)
    # أزرار التفاعل الكاملة (قبول، رفض، تأكيد، رجوع، شكاوى، تقرير)
    app.add_handler(CallbackQueryHandler(button, pattern=r"^(accept|reject|confirmreject|back|complain|report_(delivery|phone|location|other))_.+"))

    # اختيار وقت التحضير
    app.add_handler(CallbackQueryHandler(handle_time_selection, pattern=r"^time_\d+_.+"))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📊 عدد الطلبات اليوم والدخل"), handle_today_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📅 عدد الطلبات أمس والدخل"), handle_yesterday_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🗓️ طلبات الشهر الحالي"), handle_current_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📆 طلبات الشهر الماضي"), handle_last_month_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📈 طلبات السنة الحالية"), handle_current_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📉 طلبات السنة الماضية"), handle_last_year_stats))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📋 إجمالي الطلبات والدخل"), handle_total_stats))


    # ✅ تشغيل البوت
    app.run_polling()

if __name__ == '__main__':
    main()






