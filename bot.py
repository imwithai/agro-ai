import asyncio
import io
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

from google import genai
from google.genai import types as genai_types

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    logger.error("Missing TELEGRAM_BOT_TOKEN or GEMINI_API_KEY environment variables.")
    sys.exit(1)

# Initialize Bot, Dispatcher and Gemini Client
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

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

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    This handler receives messages with `/start` command
    """
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
    This handler receives photos, sends them to Gemini AI and replies with a diagnosis.
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

        # Call Gemini AI
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model='gemini-3.8-flash',
            contents=[
                AGRONOMY_PROMPT,
                genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')
            ]
        )

        # Reply with the result
        if response.text:
            await wait_message.edit_text(response.text)
        else:
            await wait_message.edit_text("❌ Kechirasiz, tahlil qilish imkoni bo'lmadi. Boshqa aniqroq rasm yuborib ko'ring.")

    except Exception as e:
        logger.error(f"Error handling photo: {e}", exc_info=True)
        await wait_message.edit_text("❌ Kechirasiz, tizimda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")


async def main() -> None:
    # Start polling
    logger.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
