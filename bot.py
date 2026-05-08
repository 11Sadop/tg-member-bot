"""
╔══════════════════════════════════════════════════╗
║     TG Member Bot v1.0                           ║
║     بوت نقل أعضاء تليجرام الاحترافي              ║
╚══════════════════════════════════════════════════╝

Telegram Bot interface for member scraping & transfer.
Deployed on Render via GitHub.
"""
import os
import sys
import asyncio
import logging
import socket
import zipfile
import shutil

# --- DNS PATCH FOR RENDER ---
def patch_dns():
    targets = {
        'api.telegram.org': '149.154.167.220',
        'production.api.telegram.org': '149.154.166.110'
    }
    old_getaddrinfo = socket.getaddrinfo
    def new_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host in targets:
            return old_getaddrinfo(targets[host], port, family, type, proto, flags)
        return old_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = new_getaddrinfo
patch_dns()
# ----------------------------

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

from telethon.errors import SessionPasswordNeededError

import database as db
import engine
from proxy_manager import proxy_manager

# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8543632979:AAFi16sozf4xqyzfElAXKulSEOcWVp_xaU0")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

# Conversation states
(
    STATE_PHONE, STATE_CODE, STATE_2FA,
    STATE_SCRAPE_SOURCE, STATE_INVITE_TARGET,
) = range(5)

# Active tasks
active_tasks = {}
stop_flags = {}


# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 تسجيل رقم جديد", callback_data="register")],
        [InlineKeyboardButton("👥 سحب الأعضاء", callback_data="scrape")],
        [InlineKeyboardButton("📤 بدء الإضافة", callback_data="invite")],
        [
            InlineKeyboardButton("📊 الحسابات", callback_data="accounts"),
            InlineKeyboardButton("📋 الإحصائيات", callback_data="stats"),
        ],
        [InlineKeyboardButton("🌐 تحديث البروكسيات المجانية", callback_data="update_proxies")],
        [InlineKeyboardButton("🗑 مسح البيانات", callback_data="clear_menu")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
    ])


def clear_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 مسح الأعضاء المحفوظين", callback_data="clear_members")],
        [InlineKeyboardButton("🗑 مسح سجل الإضافات", callback_data="clear_added")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")],
    ])


# ═══════════════════════════════════════════
# /start Command
# ═══════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    accounts = db.get_accounts()
    members = db.get_member_count()

    text = (
        f"⚡ **بوت نقل الأعضاء الاحترافي**\n\n"
        f"مرحباً {user.first_name}! 👋\n\n"
        f"📱 الحسابات المسجلة: **{len(accounts)}**\n"
        f"👥 الأعضاء المحفوظين: **{members}**\n"
        f"📊 حد الإضافة: **{engine.MAX_PER_ACCOUNT}** لكل رقم\n\n"
        f"اختر العملية من القائمة 👇"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )


# ═══════════════════════════════════════════
# Callback Router
# ═══════════════════════════════════════════
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_menu":
        await start_command(update, context)

    elif data == "register":
        await query.edit_message_text(
            "📱 **تسجيل رقم جديد**\n\n"
            "أرسل رقم الهاتف مع رمز الدولة\n"
            "مثال: `+966501234567`",
            reply_markup=back_keyboard(),
            parse_mode="Markdown",
        )
        context.user_data["state"] = STATE_PHONE

    elif data == "scrape":
        accounts = db.get_accounts()
        if not accounts:
            await query.edit_message_text(
                "❌ لا يوجد حسابات مسجلة!\n\nسجّل رقم أولاً.",
                reply_markup=back_keyboard(),
            )
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 سحب جميع الأعضاء (للمجموعات المفتوحة)", callback_data="scrape_all")],
            [InlineKeyboardButton("💬 سحب المتفاعلين (للمجموعات المخفية)", callback_data="scrape_active")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ])

        await query.edit_message_text(
            "🔍 **سحب الأعضاء**\n\nاختر نوع السحب:",
            reply_markup=kb,
            parse_mode="Markdown",
        )

    elif data in ["scrape_all", "scrape_active"]:
        context.user_data["scrape_type"] = data
        kind = "المتفاعلين من الدردشة" if data == "scrape_active" else "جميع الأعضاء"
        await query.edit_message_text(
            f"🔍 **سحب {kind}**\n\n"
            "أرسل رابط أو يوزرنيم المجموعة المصدر\n"
            "مثال: `@groupname` أو `https://t.me/groupname`",
            reply_markup=back_keyboard(),
            parse_mode="Markdown",
        )
        context.user_data["state"] = STATE_SCRAPE_SOURCE

    elif data == "invite":
        accounts = db.get_accounts()
        members_count = db.get_member_count()

        if not accounts:
            await query.edit_message_text(
                "❌ لا يوجد حسابات مسجلة!", reply_markup=back_keyboard()
            )
            return
        if members_count == 0:
            await query.edit_message_text(
                "❌ لا يوجد أعضاء محفوظين! اسحب أولاً.", reply_markup=back_keyboard()
            )
            return

        ready = [p for p in accounts if not db.is_flooded(p)]
        await query.edit_message_text(
            f"📤 **بدء الإضافة**\n\n"
            f"👥 الأعضاء: {members_count}\n"
            f"📱 حسابات جاهزة: {len(ready)}/{len(accounts)}\n"
            f"📊 الحد: {engine.MAX_PER_ACCOUNT} لكل رقم\n\n"
            f"أرسل رابط أو يوزرنيم القروب الهدف\n"
            f"مثال: `@targetgroup`",
            reply_markup=back_keyboard(),
            parse_mode="Markdown",
        )
        context.user_data["state"] = STATE_INVITE_TARGET

    elif data == "accounts":
        await show_accounts(update, context)

    elif data == "stats":
        await show_stats(update, context)

    elif data == "update_proxies":
        if proxy_manager.is_fetching:
            await query.answer("⏳ جاري تحديث البروكسيات في الخلفية بالفعل...", show_alert=True)
            return
            
        await query.edit_message_text(
            "⏳ **جاري صيد وفحص البروكسيات المجانية...**\n"
            "هذه العملية قد تستغرق دقيقة واحدة. الرجاء الانتظار.",
            parse_mode="Markdown"
        )
        
        success = await proxy_manager.run_update_cycle()
        count = proxy_manager.get_working_proxies_count()
        
        if success:
            text = f"✅ **تم تحديث البروكسيات بنجاح!**\n\n🌐 البروكسيات الجاهزة: **{count}**\n\nستقوم الحسابات باستخدامها تلقائياً عند النقل لتجنب الحظر."
        else:
            text = f"❌ **فشل في جلب بروكسيات جديدة شغال.**\n\n🌐 البروكسيات السابقة المتوفرة: **{count}**"
            
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

    elif data == "clear_menu":
        await query.edit_message_text(
            "🗑 **إدارة البيانات**\n\nاختر ما تريد مسحه:",
            reply_markup=clear_menu_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "clear_members":
        db.clear_members()
        await query.edit_message_text(
            "✅ تم مسح جميع الأعضاء المحفوظين.",
            reply_markup=back_keyboard(),
        )

    elif data == "clear_added":
        db.clear_added()
        await query.edit_message_text(
            "✅ تم مسح سجل الإضافات. يمكنك إعادة المحاولة.",
            reply_markup=back_keyboard(),
        )

    elif data == "stop_task":
        uid = update.effective_user.id
        stop_flags[uid] = True
        await query.edit_message_text(
            "⏹ جاري الإيقاف...",
            reply_markup=back_keyboard(),
        )

    elif data.startswith("proxy_"):
        phone = data.replace("proxy_", "")
        context.user_data["proxy_phone"] = phone
        await query.edit_message_text(
            f"🌐 **إعداد البروكسي للرقم {phone}**\n\n"
            f"أرسل البروكسي بالصيغ التالية:\n"
            f"`ip:port` (بدون باسورد)\n"
            f"`ip:port:user:pass` (مع باسورد)\n\n"
            f"ملاحظة: البوت يدعم SOCKS5 فقط حالياً.",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )
        context.user_data["state"] = "STATE_PROXY"

    elif data.startswith("reset_"):
        phone = data.replace("reset_", "")
        db.reset_flood(phone)
        await show_accounts(update, context)

# ═══════════════════════════════════════════
# Accounts View
# ═══════════════════════════════════════════
async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    accounts = db.get_accounts()

    if not accounts:
        text = "📱 **الحسابات**\n\nلا يوجد حسابات مسجلة."
        kb = back_keyboard()
    else:
        lines = ["📱 **الحسابات المسجلة**\n"]
        buttons = []
        for phone in accounts:
            status = db.get_account_status(phone)
            proxy_stat = "✅" if db.get_proxy(phone) else "❌"
            lines.append(f"• `{phone}` — {status} (بروكسي: {proxy_stat})")
            
            row = [InlineKeyboardButton(f"🌐 بروكسي {phone}", callback_data=f"proxy_{phone}")]
            if db.is_flooded(phone):
                row.append(InlineKeyboardButton(f"🔓 تصفير {phone}", callback_data=f"reset_{phone}"))
            buttons.append(row)

        text = "\n".join(lines)
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
        kb = InlineKeyboardMarkup(buttons)

    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


# ═══════════════════════════════════════════
# Stats View
# ═══════════════════════════════════════════
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    accounts = db.get_accounts()
    members = db.get_member_count()
    added = len(db.get_added_users())
    ready = len([p for p in accounts if not db.is_flooded(p)])

    text = (
        f"📋 **الإحصائيات**\n\n"
        f"📱 إجمالي الحسابات: {len(accounts)}\n"
        f"✅ حسابات جاهزة: {ready}\n"
        f"⏳ حسابات محظورة: {len(accounts) - ready}\n\n"
        f"👥 أعضاء محفوظين: {members}\n"
        f"📤 تمت إضافتهم: {added}\n"
        f"📊 متبقي: {max(0, members - added)}\n\n"
        f"📈 سعة الإضافة: {ready * engine.MAX_PER_ACCOUNT} عضو\n"
        f"🌐 بروكسيات مجانية جاهزة: {proxy_manager.get_working_proxies_count()}"
    )

    await query.edit_message_text(
        text, reply_markup=back_keyboard(), parse_mode="Markdown"
    )


# ═══════════════════════════════════════════
# Message Handler (Text Input)
# ═══════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text.strip()
    uid = update.effective_user.id

    # ── Phone Registration ───────────────────
    if state == STATE_PHONE:
        phone = text.replace(" ", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        context.user_data["reg_phone"] = phone
        context.user_data["state"] = STATE_CODE

        # Start registration
        status_msg = await update.message.reply_text(f"📲 جاري إرسال الكود إلى {phone}...")

        try:
            client = engine.get_client(phone.replace("+", ""))
            await client.connect()

            if await client.is_user_authorized():
                await client.disconnect()
                await status_msg.edit_text(
                    f"✅ الرقم {phone} مسجل بالفعل!",
                    reply_markup=back_keyboard()
                )
                context.user_data["state"] = None
                return

            await client.send_code_request(phone)
            context.user_data["reg_client"] = client

            await status_msg.edit_text(
                f"📨 تم إرسال كود التحقق إلى {phone}\n\n"
                f"⚠️ **تنبيه أمني هام جداً:**\n"
                f"تيليجرام يحظر تسجيل الدخول إذا أرسلت الكود بشكل مباشر.\n"
                f"يرجى إرسال الكود **مفصولاً بمسافات**.\n"
                f"مثال: إذا كان الكود `12345`، أرسله هكذا:\n"
                f"`1 2 3 4 5`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
                ]),
                parse_mode="Markdown",
            )
        except Exception as e:
            await status_msg.edit_text(
                f"❌ فشل إرسال الكود: {str(e)[:60]}",
                reply_markup=back_keyboard(),
            )
            context.user_data["state"] = None

    elif state == STATE_CODE:
        code = text.replace(" ", "").replace("-", "").strip()
        phone = context.user_data.get("reg_phone", "")
        client = context.user_data.get("reg_client")

        if not client:
            await update.message.reply_text("❌ خطأ! أعد التسجيل.", reply_markup=back_keyboard())
            context.user_data["state"] = None
            return
        
        if not code.isdigit():
            await update.message.reply_text("❌ الكود يجب أن يكون أرقاماً فقط (يُسمح بالمسافات). أرسله مرة أخرى.", reply_markup=back_keyboard())
            return

        status_msg = await update.message.reply_text("🔐 جاري التحقق...")

        try:
            await client.sign_in(phone, code)
            me = await client.get_me()
            await client.disconnect()

            await status_msg.edit_text(
                f"✅ **تم تسجيل الرقم بنجاح!**\n\n"
                f"📱 الرقم: `{phone}`\n"
                f"👤 الاسم: {me.first_name or ''} {me.last_name or ''}",
                reply_markup=back_keyboard(),
                parse_mode="Markdown",
            )
            context.user_data["state"] = None
            context.user_data.pop("reg_client", None)
            asyncio.create_task(auto_backup(context))

        except SessionPasswordNeededError:
            context.user_data["state"] = STATE_2FA
            await status_msg.edit_text(
                "🔑 الحساب محمي بكلمة مرور إضافية (2FA)\n\nأرسل كلمة المرور:"
            )

        except Exception as e:
            await client.disconnect()
            await status_msg.edit_text(
                f"❌ فشل التحقق: {str(e)[:60]}",
                reply_markup=back_keyboard(),
            )
            context.user_data["state"] = None
            context.user_data.pop("reg_client", None)

    elif state == STATE_2FA:
        password = text
        client = context.user_data.get("reg_client")
        phone = context.user_data.get("reg_phone", "")

        if not client:
            await update.message.reply_text("❌ خطأ! أعد التسجيل.", reply_markup=back_keyboard())
            context.user_data["state"] = None
            return

        status_msg = await update.message.reply_text("🔐 جاري التحقق من كلمة المرور...")

        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            await client.disconnect()

            await status_msg.edit_text(
                f"✅ **تم تسجيل الرقم بنجاح!**\n\n"
                f"📱 الرقم: `{phone}`\n"
                f"👤 الاسم: {me.first_name or ''} {me.last_name or ''}",
                reply_markup=back_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            await client.disconnect()
            await status_msg.edit_text(
                f"❌ كلمة مرور خاطئة: {str(e)[:50]}",
                reply_markup=back_keyboard(),
            )

        context.user_data["state"] = None
        context.user_data.pop("reg_client", None)
        asyncio.create_task(auto_backup(context))

    # ── Scrape ───────────────────────────────
    elif state == STATE_SCRAPE_SOURCE:
        source = text
        accounts = db.get_accounts()
        phone = accounts[0] if accounts else None

        if not phone:
            await update.message.reply_text(
                "❌ سجّل رقم أولاً!", reply_markup=back_keyboard()
            )
            context.user_data["state"] = None
            return

        status_msg = await update.message.reply_text("🔍 جاري السحب...") 
        context.user_data["state"] = None

        async def progress(msg):
            try:
                await status_msg.edit_text(msg, reply_markup=back_keyboard())
            except Exception:
                pass

        scrape_type = context.user_data.get("scrape_type")
        if scrape_type == "scrape_active":
            members, result = await engine.scrape_active_members(phone, source, progress_callback=progress)
        else:
            members, result = await engine.scrape_members(phone, source, progress_callback=progress)

        final_text = result
        if members:
            final_text += f"\n\n📊 عدد الأعضاء المحفوظين: {len(members)}"

        await status_msg.edit_text(final_text, reply_markup=back_keyboard())
        asyncio.create_task(auto_backup(context))

    # ── Invite ───────────────────────────────
    elif state == STATE_INVITE_TARGET:
        target = text
        context.user_data["state"] = None

        stop_flags[uid] = False
        stop_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ إيقاف", callback_data="stop_task")]
        ])

        status_msg = await update.message.reply_text(
            "🚀 جاري بدء الإضافة...", reply_markup=stop_kb
        )

        async def progress(msg):
            try:
                if stop_flags.get(uid):
                    kb = back_keyboard()
                else:
                    kb = stop_kb
                await status_msg.edit_text(msg, reply_markup=kb)
            except Exception:
                pass

        def check_stop():
            return stop_flags.get(uid, False)

        added, failed = await engine.invite_members(
            target, progress_callback=progress, stop_check=check_stop
        )

        stop_flags.pop(uid, None)

    # ── Proxy ────────────────────────────────
    elif state == "STATE_PROXY":
        phone = context.user_data.get("proxy_phone")
        if not phone:
            await update.message.reply_text("❌ حدث خطأ، لم يتم العثور على الرقم.", reply_markup=back_keyboard())
            context.user_data["state"] = None
            return

        proxy_str = text.strip()
        db.save_proxy(phone, proxy_str)
        
        await update.message.reply_text(
            f"✅ تم حفظ البروكسي بنجاح للرقم {phone}\n"
            f"`{proxy_str}`",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )
        context.user_data["state"] = None
        context.user_data.pop("proxy_phone", None)

    else:
        await update.message.reply_text(
            "اختر عملية من القائمة 👇",
            reply_markup=main_menu_keyboard(),
        )


# ═══════════════════════════════════════════
# /stop Command
# ═══════════════════════════════════════════
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stop_flags[uid] = True
    await update.message.reply_text("⏹ تم إرسال أمر الإيقاف.")


# ═══════════════════════════════════════════
# /accounts Command
# ═══════════════════════════════════════════
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = db.get_accounts()
    if not accounts:
        await update.message.reply_text("📱 لا يوجد حسابات مسجلة.")
        return

    lines = ["📱 **الحسابات المسجلة:**\n"]
    for phone in accounts:
        status = db.get_account_status(phone)
        lines.append(f"• `{phone}` — {status}")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ═══════════════════════════════════════════
# Backup & Restore
# ═══════════════════════════════════════════

def create_backup_zip():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(base_dir, "backup.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for folder in ['data', 'sessions']:
            folder_path = os.path.join(base_dir, folder)
            if os.path.exists(folder_path):
                for root, _, files in os.walk(folder_path):
                    for file in files:
                        abs_path = os.path.join(root, file)
                        rel_path = os.path.relpath(abs_path, base_dir)
                        zipf.write(abs_path, rel_path)
    return zip_path

async def auto_backup(context):
    if OWNER_ID == 0: return
    try:
        zip_path = create_backup_zip()
        await context.bot.send_document(
            chat_id=OWNER_ID,
            document=open(zip_path, 'rb'),
            filename="auto_backup.zip",
            caption="🔄 نسخة احتياطية تلقائية لحماية بياناتك من الحذف."
        )
        os.remove(zip_path)
    except Exception as e:
        logger.error(f"Auto backup failed: {e}")

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ هذا الأمر للمشرف فقط.")
        return
    await update.message.reply_text("📦 جاري تجهيز النسخة الاحتياطية...")
    try:
        zip_path = create_backup_zip()
        await update.message.reply_document(
            document=open(zip_path, 'rb'),
            filename="backup.zip",
            caption="📦 نسخة احتياطية من الأرقام والأعضاء\n\nلاستعادتها بعد انطفاء السيرفر، قم بالرد على هذا الملف بكلمة `/restore`."
        )
        os.remove(zip_path)
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء النسخ الاحتياطي: {e}")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ هذا الأمر للمشرف فقط.")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ قم بالرد على ملف النسخة الاحتياطية (backup.zip) بهذا الأمر.")
        return
        
    doc = update.message.reply_to_message.document
    if not doc.file_name.endswith('.zip'):
        await update.message.reply_text("❌ الملف يجب أن يكون بصيغة ZIP.")
        return
        
    await update.message.reply_text("📥 جاري استعادة البيانات...")
    try:
        file = await context.bot.get_file(doc.file_id)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        zip_path = os.path.join(base_dir, "restore.zip")
        await file.download_to_drive(zip_path)
        
        with zipfile.ZipFile(zip_path, 'r') as zipf:
            zipf.extractall(base_dir)
            
        os.remove(zip_path)
        await update.message.reply_text("✅ تم استعادة البيانات (الأرقام والأعضاء) بنجاح! يمكنك الآن تفقد الحسابات.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء الاستعادة: {e}")


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running successfully on Render.")

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

def main():
    logger.info("Starting TG Member Bot v1.0...")

    # Start dummy web server so Render doesn't timeout the "Web Service" Deploy
    threading.Thread(target=start_dummy_server, daemon=True).start()
    logger.info("Started dummy web server for Render health checks.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("accounts", accounts_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))

    # Callbacks (inline buttons)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running!")

    # Start background proxy updater
    async def proxy_updater_loop():
        while True:
            try:
                await proxy_manager.run_update_cycle()
            except Exception as e:
                logger.error(f"Proxy updater failed: {e}")
            await asyncio.sleep(7200) # Update every 2 hours
            
    # Run the background task without blocking the bot
    import sys
    if sys.version_info >= (3, 10):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
    loop.create_task(proxy_updater_loop())

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
