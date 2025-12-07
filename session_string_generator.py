# session_string_generator.py
"""
Generates a Telethon StringSession for your PERSONAL Telegram account.

Steps:
  1) Install: pip install telethon
  2) Set env vars (or create a .env if you use python-dotenv):
       API_ID=123456
       API_HASH=abcdef0123456789abcdef0123456789
  3) Run: python session_string_generator.py
  4) Enter your phone/code (and 2FA if enabled). Copy the printed string.
"""

import os
import sys
import asyncio

# Optional: load .env if present
try:
    from dotenv import load_dotenv  # pip install python-dotenv (optional)
    load_dotenv()
except Exception:
    pass

from telethon import TelegramClient
from telethon.sessions import StringSession

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"Missing {name}. Set it as an env var or in .env", file=sys.stderr)
        sys.exit(2)
    return v

API_ID  = int(need("API_ID"))
API_HASH = need("API_HASH")

async def main():
    print("\n----- Telegram Session String Generator -----\n")
    print("This script will generate a session string for your Telegram account.")
    print("You will be asked to enter your phone number and the verification code sent to your Telegram app.")
    print("Your credentials are only used locally on this machine.\n")

    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()
        print("\n=== TELETHON_STRING_SESSION ===\n")
        print(session_string)
        print("\nCopy the string above into TELETHON_STRING_SESSION (env or config.ini). Keep it secret!\n")

if __name__ == "__main__":
    asyncio.run(main())
