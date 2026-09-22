"""
gp-ext-workflow extractor
Scans all Telegram groups/channels the logged-in account is a member of,
pulls out URLs mentioned in recent messages, and writes them to output/urls.json.

Runs on GitHub Actions (cron), authenticated via a pre-generated STRING_SESSION
(no interactive login needed). See README.md for how to generate STRING_SESSION.
"""

import os
import re
import json
from datetime import datetime, timezone

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

# How many recent messages to scan per group/channel (tune as needed)
MESSAGE_LIMIT = int(os.environ.get("MESSAGE_LIMIT", "500"))

URL_REGEX = re.compile(
    r'(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>"\')\]]+',
    re.IGNORECASE,
)


def extract_urls_from_text(text):
    if not text:
        return []
    return URL_REGEX.findall(text)


def clean_url(u: str) -> str:
    # strip common trailing punctuation picked up by the regex
    return u.rstrip(".,)]}\u3002\uff0c")


def main():
    results = {}

    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue

            group_name = dialog.name
            group_id = dialog.id
            found_urls = set()

            for message in client.iter_messages(dialog, limit=MESSAGE_LIMIT):
                for u in extract_urls_from_text(message.raw_text):
                    found_urls.add(clean_url(u))

            if found_urls:
                results[str(group_id)] = {
                    "group_name": group_name,
                    "urls": sorted(found_urls),
                    "count": len(found_urls),
                }
                print(f"[{group_name}] -> {len(found_urls)} urls")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "groups": results,
        "total_groups": len(results),
        "total_urls": sum(v["count"] for v in results.values()),
    }

    os.makedirs("output", exist_ok=True)
    with open("output/urls.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"DONE: {output['total_urls']} urls from {output['total_groups']} groups")


if __name__ == "__main__":
    main()
