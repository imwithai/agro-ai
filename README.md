# Agro-Doktor AI Telegram Bot 🌾🤖

Agro-Doktor AI is a Telegram bot that helps farmers and gardeners diagnose plant diseases from photos using Google's Gemini AI.

## Features
- Diagnoses plant diseases from images of leaves, stems, or fruits.
- Powered by `gemini-2.5-flash` for fast and accurate multimodal image analysis.
- Explains the root cause, offers chemical and home treatments, and provides preventative measures.
- Completely serverless/stateless – processes each photo instantly without saving state.
- Responds in simple, clear, and farmer-friendly Uzbek.

## Requirements
- Python 3.10+
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- A Google Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))

## Installation and Setup / O'rnatish

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd agro-doktor-bot
   ```

2. **Install dependencies / Kerakli kutubxonalarni o'rnatish:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables / Muhit o'zgaruvchilarini sozlash:**
   - Copy `.env.example` to `.env`:
     ```bash
     cp .env.example .env
     ```
   - Open `.env` and fill in your keys:
     ```
     TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
     GEMINI_API_KEY=your_gemini_api_key_here
     ```

4. **Run the bot / Botni ishga tushirish:**
   ```bash
   python bot.py
   ```

## Usage
- Open your bot in Telegram and send `/start`.
- Send a photo of a sick plant (leaf, fruit, or stem).
- Wait a few seconds to receive a full diagnosis and treatment plan!

---

*Powered by `aiogram` and `google-genai`*
