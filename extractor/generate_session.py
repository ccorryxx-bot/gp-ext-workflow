"""
Run this ONCE on your own machine (not in CI) to log in interactively
and print a STRING_SESSION value. Paste that value into the STRING_SESSION
GitHub secret. Never commit the printed value.

Usage:
    pip install telethon
    python generate_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n=== STRING_SESSION (copy this into GitHub secrets) ===\n")
    print(client.session.save())
