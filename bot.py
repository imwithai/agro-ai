import asyncio
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

try:
    from dotenv import load_dotenv
    # Load environment variables
    load_dotenv()
except ImportError:
    # python-dotenv is not installed, assume env vars are set by Render
    pass

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError

# Setup logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-3.8-flash"]
REQUIRED_CHANNELS = [x.strip() for x in os.getenv("REQUIRED_CHANNELS", "").split(",") if x.strip()]

USERS_DB_FILE = "users.json"
CONFIG_DB_FILE = "config.json"

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    logger.error("Missing TELEGRAM_BOT_TOKEN or GEMINI_API_KEY environment variables.")
    sys.exit(1)

# Initialize Bot, Dispatcher and Gemini Client
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Store chat history per user ID
user_histories = {}

# Specialized agronomy prompt for Gemini (Photo Analysis)
AGRONOMY_PROMPT = """
Sen 20 yillik tajribaga ega professional agronom va o'simliklar shifokorisan.
Mijoz senga kasal bo'lgan o'simlik bargi, mevasi yoki poyasi rasmini yuboradi.
Sening vazifang - rasmga qarab o'simlik kasalligi yoki zararkunandasini aniqlash va qishloq xo'jaligiga oid oddiy va tushunarli, dehqonbop o'zbek tilida javob berish.
Javobing qisqa, tushunarli va Telegramda mobil ekranda o'qishga qulay bo'lishi kerak.
Ortiqcha uzun gaplardan saqlan. Qalin matn uchun faqat <b> so'z </b> va kursiv uchun <i> so'z </i> HTML taglaridan foydalan. Hech qachon Markdown (**, *) ishlatma!

Javobing aynan quyidagi tuzilmada va uslubda bo'lishi qat'iy talab qilinadi:

🌱 <b>AGRO-DOKTOR TASHXISI</b>

🔍 <b>1. Aniqlangan muammo:</b>
[Kasallik yoki zararkunanda nomi va foizi]

⚠️ <b>2. Kelib chiqish sababi:</b>
[1-2 jumlada asosiy sabab]

💊 <b>3. Tavsiya etilgan dori (Kimyoviy):</b>
- <i>Preparat nomi:</i> [Preparat nomi (Masalan: Skor yoki Ridomil Gold)]
- <i>Me'yori:</i> [10 litr suvga qancha solinishi]
- <i>Sepish tartibi:</i> [Qachon va qanday qo'llanilishi]

🌿 <b>4. Xalqona usul (Tabiiy yechim):</b>
[1 ta eng oson va samarali uy sharoitidagi usul]

🛡️ <b>5. Oldini olish maslahati:</b>
[Keyingi safar nima qilish kerakligi haqida qisqa eslatma]
"""

def load_db(file_path: str, default_data: dict) -> dict:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Failed to decode {file_path}. Using default data.")
    return default_data

def save_db(file_path: str, data: dict):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def get_user(user_id: int) -> dict:
    db = load_db(USERS_DB_FILE, {})
    str_id = str(user_id)
    if str_id not in db:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db[str_id] = {
            "is_vip": False,
            "vip_expires_at": None,
            "last_analysis_date": today,
            "request_count": 0,
            "referred_bonus": 0,
            "referred_by": None
        }
        save_db(USERS_DB_FILE, db)
    return db[str_id]

def update_user(user_id: int, data: dict):
    db = load_db(USERS_DB_FILE, {})
    str_id = str(user_id)
    if str_id not in db:
        get_user(user_id) # init user
        db = load_db(USERS_DB_FILE, {})
    db[str_id].update(data)
    save_db(USERS_DB_FILE, db)

def get_config() -> dict:
    return load_db(CONFIG_DB_FILE, {"payment_card": "Mavjud emas", "promo_codes": {}})

def save_config(data: dict):
    save_db(CONFIG_DB_FILE, data)

def get_subscription_keyboard(unsubscribed_channels: list[str]) -> InlineKeyboardMarkup:
    """Generates an inline keyboard with URLs to unsubscribed channels."""
    keyboard = []
    for channel in unsubscribed_channels:
        # channel starts with @, remove it for the url
        channel_username = channel.replace('@', '')
        url = f"https://t.me/{channel_username}"
        keyboard.append([InlineKeyboardButton(text="➕ Kanalga a'zo bo'lish", url=url)])

    keyboard.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def require_subscription(message: Message) -> bool:
    """Checks subscription and prompts user if needed. Returns True if block should occur."""
    unsubscribed = await get_unsubscribed_channels(message.from_user.id)
    if unsubscribed:
        await message.answer(
            "🛑 <b>Botdan foydalanish uchun rasmiy kanalimizga obuna bo'ling!</b>\n\n"
            "Obuna bo'lgach, pastdagi <b>«✅ Obunani tekshirish»</b> tugmasini bosing.",
            reply_markup=get_subscription_keyboard(unsubscribed)
        )
        return True
    return False

async def get_unsubscribed_channels(user_id: int) -> list[str]:
    """Returns a list of required channels the user is not subscribed to."""
    if not REQUIRED_CHANNELS or user_id in ADMIN_IDS:
        return []

    unsubscribed = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                unsubscribed.append(channel)
        except TelegramAPIError as e:
            logger.warning(f"Error checking sub for {channel}: {e}. Skipping check.")
            # If bot is not admin in channel, allow bypass so it doesn't break
    return unsubscribed

USERS_FILE = "users.txt"

def add_user(user_id: int):
    """Adds a user ID to users.txt if not already present."""
    users = set()
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            users = set(f.read().splitlines())

    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(f"{user_id}\n")

class BroadcastState(StatesGroup):
    waiting_for_message = State()

class PaymentState(StatesGroup):
    waiting_for_receipt = State()

class AdminConfigState(StatesGroup):
    waiting_for_card = State()
    waiting_for_promo_code = State()
    waiting_for_vip_id = State()
    waiting_for_revoke_id = State()

RECEIPT_PROMPT = """
Siz bank to'lovi cheki (skrinshot)ni ko'rib turibsiz.
Iltimos, ushbu chekning to'g'riligini tekshiring:
1. Summasi kamida 19000 so'm ekanligiga ishonch hosil qiling.
2. O'tkazma yaqin vaqt ichida bo'lganini tekshiring.

Javobingizni quyidagi formatda bering (faqat shu formatda):
Holati: [Yaroqli/Yaroqsiz]
Sababi: [Nima uchun yaroqli yoki yaroqsiz ekanligi haqida qisqacha izoh]
Summasi: [O'tkazma summasi]
"""

def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Reklama tarqatish", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="💳 Karta raqamini o'zgartirish", callback_data="admin_setcard")],
            [InlineKeyboardButton(text="🎁 Promo kod yaratish", callback_data="admin_promo")],
            [InlineKeyboardButton(text="⭐ VIP berish", callback_data="admin_grantvip"), InlineKeyboardButton(text="🚫 VIP olish", callback_data="admin_revokevip")],
            [InlineKeyboardButton(text="🛠 Qollab-quvvatlash", callback_data="admin_support")]
        ]
    )

@dp.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    """Admin panel handler."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 <b>Admin Paneliga xush kelibsiz!</b>", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Siz admin emassiz!", show_alert=True)
        return

    action = callback.data.split("_")[1]

    if action == "stats":
        users = []
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                users = f.read().splitlines()
        await callback.answer()
        await callback.message.answer(f"📊 <b>Statistika:</b>\n\nJami foydalanuvchilar: {len(users)} ta")

    elif action == "broadcast":
        await callback.answer()
        await state.set_state(BroadcastState.waiting_for_message)
        await callback.message.answer("📢 <b>Reklama tarqatish</b>\n\nTarqatmoqchi bo'lgan xabaringizni yuboring (matn, rasm yoki video):", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_admin")]]
        ))

    elif action == "setcard":
        await callback.answer()
        await state.set_state(AdminConfigState.waiting_for_card)
        await callback.message.answer("💳 <b>Karta raqamini kiriting:</b>", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_admin")]]
        ))

    elif action == "promo":
        await callback.answer()
        await state.set_state(AdminConfigState.waiting_for_promo_code)
        await callback.message.answer("🎁 <b>Yangi promo kodni kiriting (kod,tur):</b>\nTur: `vip` yoki `bonus`\nMasalan: `NEWYEAR2024,vip`", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_admin")]]
        ))

    elif action == "grantvip":
        await callback.answer()
        await state.set_state(AdminConfigState.waiting_for_vip_id)
        await callback.message.answer("⭐ <b>Foydalanuvchi ID sini kiriting (VIP berish uchun):</b>", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_admin")]]
        ))

    elif action == "revokevip":
        await callback.answer()
        await state.set_state(AdminConfigState.waiting_for_revoke_id)
        await callback.message.answer("🚫 <b>Foydalanuvchi ID sini kiriting (VIP olish uchun):</b>", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_admin")]]
        ))

    elif action == "support":
        await callback.answer()
        await callback.message.answer("🛠 <b>Qo'llab-quvvatlash</b>\n\nFoydalanuvchilar savol yo'llashlari uchun admin bilan bog'lanish:\n@AdminUsername (sozlash mumkin)")

@dp.message(AdminConfigState.waiting_for_card)
async def process_setcard(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    config = get_config()
    config["payment_card"] = message.text
    save_config(config)
    await state.clear()
    await message.answer(f"✅ Karta raqami o'zgartirildi:\n<code>{message.text}</code>")

@dp.message(AdminConfigState.waiting_for_promo_code)
async def process_promo_code_creation(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        code, reward = message.text.split(",")
        code = code.strip()
        reward = reward.strip().lower()
        if reward not in ["vip", "bonus"]:
            raise ValueError
        config = get_config()
        if "promo_codes" not in config:
            config["promo_codes"] = {}
        config["promo_codes"][code] = reward
        save_config(config)
        await state.clear()
        await message.answer(f"✅ Promo kod yaratildi: <code>{code}</code> ({reward})")
    except Exception:
        await message.answer("❌ Xato format. Masalan: `KOD123,vip`")

@dp.message(AdminConfigState.waiting_for_vip_id)
async def process_grant_vip(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
        from datetime import timedelta
        expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        update_user(user_id, {"is_vip": True, "vip_expires_at": expiry})
        await state.clear()
        await message.answer(f"✅ {user_id} foydalanuvchiga 30 kunlik VIP berildi.")
    except Exception:
        await message.answer("❌ Noto'g'ri ID kiritildi.")

@dp.message(AdminConfigState.waiting_for_revoke_id)
async def process_revoke_vip(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
        update_user(user_id, {"is_vip": False, "vip_expires_at": None})
        await state.clear()
        await message.answer(f"✅ {user_id} foydalanuvchidan VIP olib tashlandi.")
    except Exception:
        await message.answer("❌ Noto'g'ri ID kiritildi.")

@dp.callback_query(F.data == "cancel_admin")
async def cancel_admin_state(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await callback.message.edit_text("❌ Amal bekor qilindi.")
    await callback.answer()

@dp.callback_query(F.data == "buy_vip")
async def buy_vip_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PaymentState.waiting_for_receipt)
    await callback.message.answer("📸 Iltimos, tolov chekini (skrinshotni) shu yerga yuboring:", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_payment")]]
    ))

@dp.callback_query(F.data == "cancel_payment")
async def cancel_payment(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Tolov bekor qilindi.")
    await callback.answer()

@dp.message(PaymentState.waiting_for_receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext) -> None:
    """Processes the uploaded payment receipt."""
    wait_message = await message.answer("🔍 Chek tekshirilmoqda, iltimos kuting...")

    try:
        photo_meta = message.photo[-1]
        file_info = await bot.get_file(photo_meta.file_id)
        image_stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=image_stream)

        image_bytes = image_stream.getvalue()
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

        parts = [image_part, genai_types.Part.from_text(text=RECEIPT_PROMPT)]

        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.8-flash',
            contents=parts
        )

        if response.text and "Holati: Yaroqli" in response.text:
            # Verified by AI, send to admin for final approval
            await wait_message.edit_text("✅ Chek AI tomonidan tasdiqlandi. Adminga yakuniy tasdiqlash uchun yuborildi.")

            admin_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_vip_{message.from_user.id}"),
                        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_vip_{message.from_user.id}")
                    ]
                ]
            )

            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=photo_meta.file_id,
                        caption=f"💳 <b>Yangi VIP tolov!</b>\nFoydalanuvchi: {message.from_user.full_name} (ID: <code>{message.from_user.id}</code>)\n\n<b>AI Xulosasi:</b>\n{response.text}",
                        reply_markup=admin_keyboard
                    )
                except Exception:
                    pass
        else:
            await wait_message.edit_text(f"❌ Chek AI tomonidan tasdiqlanmadi yoki xatolik mavjud.\n\n<b>AI Xulosasi:</b>\n{response.text}\n\nIltimos, qayta urinib ko'ring yoki adminga murojaat qiling.")

        await state.clear()

    except Exception as e:
        logger.error(f"Error handling receipt: {e}", exc_info=True)
        await wait_message.edit_text("❌ Tizimda xatolik yuz berdi. Iltimos, qayta urinib ko'ring.")
        await state.clear()

@dp.callback_query(F.data.startswith("approve_vip_"))
async def approve_vip_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    user_id = int(callback.data.split("_")[2])
    from datetime import timedelta
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    update_user(user_id, {"is_vip": True, "vip_expires_at": expiry})

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Foydalanuvchi {user_id} VIP ga o'tkazildi.")

    try:
        await bot.send_message(user_id, "🎉 <b>Tabriklaymiz!</b> Sizning VIP tolovingiz admin tomonidan tasdiqlandi. Siz endi 30 kun davomida VIP imtiyozlaridan foydalanishingiz mumkin!")
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_vip_"))
async def reject_vip_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    user_id = int(callback.data.split("_")[2])

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ Foydalanuvchi {user_id} tolovi rad etildi.")

    try:
        await bot.send_message(user_id, "❌ <b>Tolov rad etildi.</b> Iltimos, adminga murojaat qiling yoki qaytadan tolovni amalga oshiring.")
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await callback.message.edit_text("❌ Reklama tarqatish bekor qilindi.")
    await callback.answer()

@dp.callback_query(F.data == "check_subs")
async def check_subs_callback(callback: CallbackQuery):
    unsubscribed = await get_unsubscribed_channels(callback.from_user.id)
    if not unsubscribed:
        await callback.message.delete()
        await callback.message.answer("✅ <b>Rahmat! Obuna tasdiqlandi. Endi kasallangan o'simlik rasmini yuborishingiz mumkin!</b>")
        await callback.answer()
    else:
        await callback.answer("❌ Siz hali kanalga obuna bo'lmadingiz!", show_alert=True)

@dp.message(Command("help"))
async def help_command_handler(message: Message) -> None:
    """Help command for users to get support."""
    await message.answer(
        "🛠 <b>Qo'llab-quvvatlash va Qo'llanma</b>\n\n"
        "1️⃣ <b>Botdan foydalanish:</b> Ekinlaringiz yoki bargdagi kasallikni ko'rsatuvchi rasmni botga yuboring.\n"
        "2️⃣ <b>Limitlar:</b> Har kuni 1 ta bepul so'rov mavjud. Qo'shimcha so'rovlar uchun do'stlaringizni taklif qiling (/referral) yoki VIP obuna oling (/vip).\n\n"
        "Savollaringiz bo'lsa, adminga murojaat qilishingiz mumkin:\n"
        "👉 @AdminUsername"
    )

@dp.message(Command("vip"))
@dp.message(Command("pricing"))
async def vip_command_handler(message: Message) -> None:
    """Displays VIP subscription info."""
    config = get_config()
    card_number = config.get("payment_card", "Mavjud emas")

    text = (
        "💎 <b>VIP Obuna Sotib Olish</b>\n\n"
        "Cheksiz so'rovlar va botdan erkin foydalanish uchun VIP obuna xarid qiling!\n\n"
        "💰 <b>Narxi:</b> 19,000 UZS (30 kun uchun)\n"
        f"💳 <b>Karta raqami:</b> <code>{card_number}</code>\n\n"
        "⚠️ <b>Diqqat:</b>\n"
        "- Tolovdan so'ng pul qaytarilmaydi.\n"
        "- Chek (skrinshot)ni yuboring. AI uni avtomatik tekshirib adminga tasdiqlash uchun yuboradi."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 VIP Obuna Sotib Olish", callback_data="buy_vip")]]
    )
    await message.answer(text, reply_markup=keyboard)

@dp.message(Command("referral"))
async def referral_command_handler(message: Message) -> None:
    """Displays user's referral link."""
    user = get_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=REF_{message.from_user.id}"

    text = (
        "👥 <b>Do'stlarni taklif qilish</b>\n\n"
        f"Sizning taklif havolangiz:\n<code>{ref_link}</code>\n\n"
        "Har bir taklif qilingan foydalanuvchi uchun <b>+1 ta bepul so'rov</b> bonusi olasiz!\n"
        f"🏆 <b>Sizning bonuslaringiz:</b> {user.get('referred_bonus', 0)} ta"
    )
    await message.answer(text)

@dp.message(Command("privacy"))
async def privacy_command_handler(message: Message) -> None:
    """Privacy and Terms of Use command handler."""
    privacy_text = (
        "🔐 <b>Maxfiylik va Xavfsizlik</b>\n"
        "Sizning xavfsizligingiz biz uchun muhim. Siz yuborgan rasmlar va xabarlar hech qayerda saqlanmaydi va faqatgina o'sha vaqtning o'zida tahlil qilish uchun ishlatiladi.\n\n"
        "⚠️ <b>Ogohlantirish va Mas'uliyat</b>\n"
        "Ushbu bot Sun'iy Intellekt (AI) yordamida ishlaydi va uning javoblari 100% aniq bo'lmasligi yoki xatoliklarga yo'l qo'yishi mumkin. "
        "Bot yaratuvchilari va ma'murlari sizning ekinlaringiz hosildorligi, holati yoki qo'llanilgan kimyoviy moddalar natijasida kelib chiqadigan har qanday zararlar uchun <b>HECH QANDAY JAVOBGARLIKNI O'Z ZIMMASIGA OLMAYDI</b>.\n"
        "Kuchli kimyoviy moddalarni qo'llashdan oldin, albatta mahalliy agronom mutaxassislar bilan maslahatlashishingizni tavsiya qilamiz.\n\n"
        "📜 <b>Foydalanish shartlari</b>\n"
        "Botdan foydalanish orqali siz ushbu shartlarga rozilik bildirasiz."
    )
    await message.answer(privacy_text)

@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    """Handles the message sent by admin for broadcast."""
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    users = []
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            users = f.read().splitlines()

    if not users:
        await message.answer("❌ Foydalanuvchilar ro'yxati bo'sh!")
        return

    status_msg = await message.answer("⏳ Reklama tarqatilmoqda... Iltimos kuting.")

    success_count = 0
    fail_count = 0

    for user_id in users:
        try:
            await message.send_copy(chat_id=user_id)
            success_count += 1
            await asyncio.sleep(0.05) # Prevent hitting rate limits
        except Exception:
            fail_count += 1

    await status_msg.edit_text(
        f"✅ <b>Reklama tarqatish yakunlandi!</b>\n\n"
        f"Muvaffaqiyatli: {success_count} ta\n"
        f"Xatolik (Bloklaganlar): {fail_count} ta"
    )

def check_user_limits(user_id: int) -> bool:
    """Returns True if the user can make a request, False if they hit the limit."""
    user = get_user(user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if user["is_vip"]:
        # Check expiration
        if user["vip_expires_at"]:
            expiry = datetime.fromisoformat(user["vip_expires_at"])
            if datetime.now(timezone.utc) <= expiry:
                return True
            else:
                update_user(user_id, {"is_vip": False, "vip_expires_at": None})
                return False # Let them fall through to free limits
        return True # Lifetime VIP fallback

    if user["last_analysis_date"] != today:
        update_user(user_id, {"last_analysis_date": today, "request_count": 0})
        user["request_count"] = 0

    # Free users get 1 free request + bonus requests
    total_allowed = 1 + user["referred_bonus"]
    if user["request_count"] < total_allowed:
        return True
    return False

def consume_request(user_id: int):
    """Increments the user's request count."""
    user = get_user(user_id)
    if not user["is_vip"]:
        update_user(user_id, {"request_count": user["request_count"] + 1})


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    This handler receives messages with `/start` command, processes referrals.
    """
    user_id = message.from_user.id
    db = load_db(USERS_DB_FILE, {})
    is_new_user = str(user_id) not in db

    user = get_user(user_id) # Initializes user

    # Check for referral
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    if is_new_user and args and args[0].startswith("REF_"):
        referrer_id_str = args[0].replace("REF_", "")
        if referrer_id_str.isdigit() and str(referrer_id_str) != str(user_id):
            referrer_id = int(referrer_id_str)
            referrer = get_user(referrer_id)
            update_user(user_id, {"referred_by": referrer_id})
            update_user(referrer_id, {"referred_bonus": referrer.get("referred_bonus", 0) + 1})
            try:
                await bot.send_message(referrer_id, "🎉 <b>Tabriklaymiz!</b> Yangi do'stingiz sizning havolangiz orqali kirdi. Sizga +1 ta bepul so'rov bonusi berildi!")
            except Exception:
                pass # User might have blocked bot

    if await require_subscription(message):
        return

    welcome_text = (
        "👋 Assalomu alaykum! Men Agro-Doktor AI - sun'iy intellektga asoslangan o'simliklar shifokoriman.\n\n"
        "🌱 Ekinlaringiz, gullaringiz yoki daraxtlaringiz kasallangan bo'lsa, menga ularning bargi, mevasi yoki poyasi <b>rasmini yuboring</b>.\n"
        "Men darhol kasallikni aniqlayman va uni davolash rejasini taqdim etaman!\n\n"
        "📸 Iltimos, muammoni aniq ko'rsatadigan rasm yuboring."
    )
    await message.answer(welcome_text)

@dp.message(Command("promo"))
async def promo_command_handler(message: Message) -> None:
    """Handles promo code redemption."""
    args = message.text.split()[1:]
    if not args:
        await message.answer("Iltimos, promo kodni kiriting. Masalan: `/promo KOD123`")
        return

    code = args[0]
    config = get_config()
    promos = config.get("promo_codes", {})

    if code in promos:
        reward = promos[code]
        if reward == "vip":
            from datetime import timedelta
            expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            update_user(message.from_user.id, {"is_vip": True, "vip_expires_at": expiry})
            await message.answer("🎉 <b>Promo kod qabul qilindi!</b> Sizga 30 kunlik VIP maqom berildi.")
        elif reward == "bonus":
            user = get_user(message.from_user.id)
            update_user(message.from_user.id, {"referred_bonus": user.get("referred_bonus", 0) + 1})
            await message.answer("🎉 <b>Promo kod qabul qilindi!</b> Sizga +1 ta bepul so'rov bonusi berildi.")

        # Remove promo code (single use)
        del promos[code]
        save_config(config)
    else:
        await message.answer("❌ Noto'g'ri yoki ishlatilgan promo kod.")



@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    """
    This handler receives photos (with or without captions), adds them to the chat context, and replies with a diagnosis.
    """
    if await require_subscription(message):
        return

    if not check_user_limits(message.from_user.id):
        await message.answer("❌ Kunlik bepul limit tugadi!\n\nIltimos, qo'shimcha so'rovlar uchun do'stlaringizni taklif qiling (/referral) yoki VIP obuna xarid qiling (/vip).")
        return

    wait_message = await message.answer("🔍 Rasm tahlil qilinmoqda, iltimos kuting...")

    try:
        # Get the highest resolution photo
        photo_meta = message.photo[-1]

        # Download the photo as bytes
        file_info = await bot.get_file(photo_meta.file_id)
        image_stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=image_stream)

        image_bytes = image_stream.getvalue()
        if not image_bytes:
            await wait_message.edit_text("❌ Rasmni yuklab olishda xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
            return

        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

        # Prepare parts statelessly
        parts = [image_part]
        if message.caption:
            parts.append(genai_types.Part.from_text(text=message.caption))
        else:
            parts.append(genai_types.Part.from_text(text="Iltimos ushbu rasmdagi o'simlikni tahlil qilib bering."))

        # Call Gemini AI statelessly with fallback models loop
        response = None
        last_api_error = None

        for model_name in MODELS:
            try:
                logger.info(f"Trying model: {model_name}")
                response = await gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=parts,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=AGRONOMY_PROMPT
                    )
                )
                break  # Success, exit the fallback loop
            except APIError as e:
                logger.warning(f"Model {model_name} failed with APIError: {e}")
                last_api_error = e
                continue # Try the next model

        if response and response.text:
            consume_request(message.from_user.id)
            final_text = response.text + "\n\n⚠️ <i>Diqqat: Sun'iy intellekt xato qilishi mumkin. Ekinlarga ishlov berishdan oldin mutaxassis bilan maslahatlashing.</i>"
            try:
                await wait_message.edit_text(final_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"Failed to parse HTML, falling back to plain text: {e}")
                await wait_message.edit_text(final_text, parse_mode=None)
        elif last_api_error:
            # Handle the case where all models failed
            error_str = str(last_api_error)
            if "429" in error_str or "503" in error_str:
                logger.warning(f"All models failed due to rate limits or overload: {last_api_error}")
                await wait_message.edit_text("⏳ Hozirda bepul so'rovlar limiti vaqtincha to'ldi. Iltimos, 1 daqiqadan so'ng qayta yuboring.")
            else:
                logger.error(f"All models failed with API Error: {last_api_error}", exc_info=True)
                error_msg = str(last_api_error)[:150]
                await wait_message.edit_text(f"❌ API Xatolik: {error_msg}...")
        else:
            await wait_message.edit_text("❌ Kechirasiz, tahlil qilish imkoni bo'lmadi. Boshqa aniqroq rasm yuborib ko'ring.")

    except Exception as e:
        logger.error(f"Error handling photo: {e}", exc_info=True)
        error_msg = str(e)[:150]
        await wait_message.edit_text(f"❌ Tizimda xatolik: {error_msg}...")


@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text_fallback(message: Message) -> None:
    """Fallback handler for text messages telling users to send a photo."""
    await message.answer(
        "🌱 Kechirasiz, men matnli savollarga javob bera olmayman.\n\n"
        "Iltimos, ekinlaringiz yoki o'simliklaringizdagi kasallikni aniqlash uchun menga ularning <b>rasmini yuboring</b> 📸"
    )

async def health_check(request: web.Request) -> web.Response:
    """Minimal health check endpoint for Render."""
    return web.Response(text="Agro-Doktor AI Bot is running.")

async def start_web_server():
    """Starts a minimal aiohttp web server."""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    logger.info(f"Starting web server on port {port}")
    await site.start()

async def main() -> None:
    # Drop pending updates to avoid webhook conflict
    await bot.delete_webhook(drop_pending_updates=True)

    # Start web server and bot polling concurrently
    logger.info("Starting bot and web server...")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
