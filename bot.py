import asyncio
import io
import logging
import os
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    logger.error("Missing TELEGRAM_BOT_TOKEN or GEMINI_API_KEY environment variables.")
    sys.exit(1)

# Initialize Bot, Dispatcher and Gemini Client
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Store chat history per user ID
user_histories = {}

# Specialized agronomy prompt for Gemini
AGRONOMY_PROMPT = """
Sen 20 yillik tajribaga ega professional agronom va o'simliklar shifokorisan.
Mijoz senga kasal bo'lgan o'simlik bargi, mevasi yoki poyasi rasmini yuboradi.
Sening vazifang - rasmga qarab o'simlik kasalligi yoki zararkunandasini aniqlash va qishloq xo'jaligiga oid oddiy va tushunarli, dehqonbop o'zbek tilida javob berish.
Javobing qisqa, tushunarli va Telegramda mobil ekranda o'qishga qulay bo'lishi kerak.
Ortiqcha uzun gaplardan saqlan. Asosiy atamalarni qalin qilib yoz.

Javobing aynan quyidagi tuzilmada va uslubda bo'lishi qat'iy talab qilinadi:

🌱 **AGRO-DOKTOR TASHXISI**

🔍 **1. Aniqlangan muammo:**
[Kasallik yoki zararkunanda nomi va foizi]

⚠️ **2. Kelib chiqish sababi:**
[1-2 jumlada asosiy sabab]

💊 **3. Tavsiya etilgan dori (Kimyoviy):**
- *Preparat nomi:* [Preparat nomi (Masalan: Skor yoki Ridomil Gold)]
- *Me'yori:* [10 litr suvga qancha solinishi]
- *Sepish tartibi:* [Qachon va qanday qo'llanilishi]

🌿 **4. Xalqona usul (Tabiiy yechim):**
[1 ta eng oson va samarali uy sharoitidagi usul]

🛡️ **5. Oldini olish maslahati:**
[Keyingi safar nima qilish kerakligi haqida qisqa eslatma]
"""

def get_user_history(user_id: int):
    """Gets the chat history for a user, maintaining stateless context."""
    if user_id not in user_histories:
        user_histories[user_id] = []
    return user_histories[user_id]

def update_user_history(user_id: int, role: str, parts: list):
    """Appends a new message to the user's history and truncates if too long."""
    history = get_user_history(user_id)
    history.append(genai_types.Content(role=role, parts=parts))
    # Keep history manageable (e.g., last 10 messages) to prevent token limits
    # Gemini requires alternating roles and the first role must be 'user'
    if len(history) > 10:
        sliced_history = history[-10:]
        if sliced_history[0].role != "user":
            sliced_history = sliced_history[1:]
        user_histories[user_id] = sliced_history

def pop_last_user_message(user_id: int):
    """Removes the last user message from history on API failure to prevent back-to-back user roles."""
    history = get_user_history(user_id)
    if history and history[-1].role == "user":
        history.pop()

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
    waiting_for_support_message = State()

def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Reklama tarqatish", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🛠 Qollab-quvvatlash", callback_data="admin_support")]
        ]
    )

@dp.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    """Admin panel handler."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 *Admin Paneliga xush kelibsiz!*", reply_markup=get_admin_keyboard())

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
        await callback.message.answer(f"📊 *Statistika:*\n\nJami foydalanuvchilar: {len(users)} ta")

    elif action == "broadcast":
        await callback.answer()
        await state.set_state(BroadcastState.waiting_for_message)
        await callback.message.answer("📢 *Reklama tarqatish*\n\nTarqatmoqchi bo'lgan xabaringizni yuboring (matn, rasm yoki video):", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_broadcast")]]
        ))

    elif action == "support":
        await callback.answer()
        await callback.message.answer("🛠 *Qo'llab-quvvatlash*\n\nFoydalanuvchilar savol yo'llashlari uchun admin bilan bog'lanish:\n@AdminUsername (sozlash mumkin)")

@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await callback.message.edit_text("❌ Reklama tarqatish bekor qilindi.")
    await callback.answer()

@dp.message(Command("help"))
async def help_command_handler(message: Message) -> None:
    """Help command for users to get support."""
    await message.answer(
        "🛠 *Qo'llab-quvvatlash bo'limi*\n\n"
        "Savollaringiz yoki takliflaringiz bo'lsa, adminga murojaat qilishingiz mumkin:\n"
        "👉 @AdminUsername"
    )

@dp.message(Command("privacy"))
async def privacy_command_handler(message: Message) -> None:
    """Privacy and Terms of Use command handler."""
    privacy_text = (
        "🔐 *Maxfiylik va Xavfsizlik*\n"
        "Sizning xavfsizligingiz biz uchun muhim. Siz yuborgan rasmlar va xabarlar hech qayerda saqlanmaydi va faqatgina o'sha vaqtning o'zida tahlil qilish uchun ishlatiladi.\n\n"
        "⚠️ *Ogohlantirish va Mas'uliyat*\n"
        "Ushbu bot Sun'iy Intellekt (AI) yordamida ishlaydi va uning javoblari 100% aniq bo'lmasligi yoki xatoliklarga yo'l qo'yishi mumkin. "
        "Bot yaratuvchilari va ma'murlari sizning ekinlaringiz hosildorligi, holati yoki qo'llanilgan kimyoviy moddalar natijasida kelib chiqadigan har qanday zararlar uchun **HECH QANDAY JAVOBGARLIKNI O'Z ZIMMASIGA OLMAYDI**.\n"
        "Kuchli kimyoviy moddalarni qo'llashdan oldin, albatta mahalliy agronom mutaxassislar bilan maslahatlashishingizni tavsiya qilamiz.\n\n"
        "📜 *Foydalanish shartlari*\n"
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
        f"✅ *Reklama tarqatish yakunlandi!*\n\n"
        f"Muvaffaqiyatli: {success_count} ta\n"
        f"Xatolik (Bloklaganlar): {fail_count} ta"
    )

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    This handler receives messages with `/start` command
    """
    add_user(message.from_user.id)

    welcome_text = (
        "👋 Assalomu alaykum! Men Agro-Doktor AI - sun'iy intellektga asoslangan o'simliklar shifokoriman.\n\n"
        "🌱 Ekinlaringiz, gullaringiz yoki daraxtlaringiz kasallangan bo'lsa, menga ularning bargi, mevasi yoki poyasi **rasmini yuboring**.\n"
        "Men darhol kasallikni aniqlayman va uni davolash rejasini taqdim etaman!\n\n"
        "📸 Iltimos, muammoni aniq ko'rsatadigan rasm yuboring."
    )
    await message.answer(welcome_text)


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    """
    This handler receives photos (with or without captions), adds them to the chat context, and replies with a diagnosis.
    """
    wait_message = await message.answer("🔍 Rasm tahlil qilinmoqda, iltimos kuting...")

    try:
        # Get the highest resolution photo
        photo_meta = message.photo[-1]

        # Download the photo as bytes
        file_info = await bot.get_file(photo_meta.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)

        if not downloaded_file:
            await wait_message.edit_text("❌ Rasmni yuklab olishda xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
            return

        image_bytes = downloaded_file.read()
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

        # Prepare parts
        parts = [image_part]
        if message.caption:
            parts.append(genai_types.Part.from_text(text=message.caption))
        else:
            parts.append(genai_types.Part.from_text(text="Iltimos ushbu rasmdagi o'simlikni tahlil qilib bering."))

        # Add to history
        user_id = message.from_user.id
        update_user_history(user_id, "user", parts)

        # Call Gemini AI statelessly with history and system instruction
        history = get_user_history(user_id)
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.8-flash',
            contents=history,
            config=genai_types.GenerateContentConfig(
                system_instruction=AGRONOMY_PROMPT
            )
        )

        # Reply with the result
        if response.text:
            update_user_history(user_id, "model", [genai_types.Part.from_text(text=response.text)])
            await wait_message.edit_text(response.text)
        else:
            pop_last_user_message(user_id)
            await wait_message.edit_text("❌ Kechirasiz, tahlil qilish imkoni bo'lmadi. Boshqa aniqroq rasm yuborib ko'ring.")

    except APIError as e:
        pop_last_user_message(message.from_user.id)
        if "503" in str(e):
            logger.warning(f"Gemini API 503 Service Unavailable: {e}")
            await wait_message.edit_text("⏳ Tizimda yuklama yuqori (503 Xatolik). Iltimos, bir necha daqiqadan so'ng qayta urinib ko'ring.")
        else:
            logger.error(f"Gemini API Error: {e}", exc_info=True)
            await wait_message.edit_text("❌ Kechirasiz, tizimda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")
    except Exception as e:
        pop_last_user_message(message.from_user.id)
        logger.error(f"Error handling photo: {e}", exc_info=True)
        await wait_message.edit_text("❌ Kechirasiz, tizimda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")

@dp.message(F.text)
async def handle_text(message: Message) -> None:
    """
    This handler receives text-only follow-up questions from the user and maintains conversational context.
    """
    wait_message = await message.answer("✍️ Yozilmoqda...")

    try:
        user_id = message.from_user.id
        parts = [genai_types.Part.from_text(text=message.text)]
        update_user_history(user_id, "user", parts)

        history = get_user_history(user_id)
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.8-flash',
            contents=history,
            config=genai_types.GenerateContentConfig(
                system_instruction=AGRONOMY_PROMPT
            )
        )

        if response.text:
            update_user_history(user_id, "model", [genai_types.Part.from_text(text=response.text)])
            await wait_message.edit_text(response.text)
        else:
            pop_last_user_message(user_id)
            await wait_message.edit_text("❌ Kechirasiz, javob shakllantirib bo'lmadi.")

    except APIError as e:
        pop_last_user_message(message.from_user.id)
        if "503" in str(e):
            logger.warning(f"Gemini API 503 Service Unavailable: {e}")
            await wait_message.edit_text("⏳ Tizimda yuklama yuqori (503 Xatolik). Iltimos, bir necha daqiqadan so'ng qayta urinib ko'ring.")
        else:
            logger.error(f"Gemini API Error: {e}", exc_info=True)
            await wait_message.edit_text("❌ Kechirasiz, tizimda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")
    except Exception as e:
        pop_last_user_message(message.from_user.id)
        logger.error(f"Error handling text: {e}", exc_info=True)
        await wait_message.edit_text("❌ Kechirasiz, tizimda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")


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
