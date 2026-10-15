import asyncio
from google import genai
from google.genai import types

async def main():
    # just testing the import and object creation, we don't have a valid API key but we can see if it raises a method missing error
    client = genai.Client(api_key="fake")
    try:
        chat = client.aio.chats.create(
            model="gemini-3.8-flash",
            config=types.GenerateContentConfig(system_instruction="You are a bot")
        )
        print("Chat created:", type(chat))
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
