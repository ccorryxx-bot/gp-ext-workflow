"""Offline tests for run()'s failure handling -- no Telegram, no Cloudflare.

Run from extractor/:   python -m unittest discover -s tests -v

Covers the incident class seen on the NCH account: one group becoming
inaccessible (ChannelPrivateError) mid-run used to kill the whole multi-hour
run AND lose all unsaved progress.
"""
import copy
import json
import os
import sys
import time
import types as pytypes
import unittest
from datetime import datetime, timedelta, timezone

for k, v in {
    "API_ID": "1", "API_HASH": "x", "STRING_SESSION": "", "BOT_TOKEN": "x", "BOT_CHAT_ID": "1",
    "CF_ACCOUNT_ID": "x", "CF_API_TOKEN": "x", "CF_KV_NAMESPACE_ID": "x", "ACCOUNT": "test",
}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extract  # noqa: E402
from telethon.errors import ChannelPrivateError, AuthKeyDuplicatedError  # noqa: E402


def dialog(did, name):
    return pytypes.SimpleNamespace(
        id=did, name=name, is_group=True, is_channel=False,
        entity=pytypes.SimpleNamespace(participants_count=2000),
    )


def msg(mid, text):
    return pytypes.SimpleNamespace(id=mid, raw_text=text)


class FakeClient:
    def __init__(self, dialogs, behaviors, slow=()):
        self.dialogs, self.behaviors, self.slow = dialogs, behaviors, set(slow)
        self.calls = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_dialogs(self):
        return iter(self.dialogs)

    def get_messages(self, dialog, **kw):
        return pytypes.SimpleNamespace(total=0)

    def iter_messages(self, dialog, min_id=0, reverse=True, limit=20):
        self.calls[dialog.id] = self.calls.get(dialog.id, 0) + 1
        if dialog.id in self.slow:
            time.sleep(1.2)
        beh = self.behaviors[dialog.id]
        if isinstance(beh, BaseException):
            raise beh
        return iter([m for m in beh if m.id > min_id][:limit])


class Base(unittest.TestCase):
    def setUp(self):
        self.store, self.sent, self.batches, self.state_writes = {}, [], [], [0]
        extract.FAIL_CONTEXT.clear()
        self._orig = {n: getattr(extract, n) for n in (
            "kv_get", "kv_put", "_send_telegram_message", "_edit_telegram_message", "send_url_batch",
            "jitter_sleep", "safe_sleep", "batch_rest_sleep", "TelegramClient", "VALIDATE_URLS",
            "PERSIST_INTERVAL_SECONDS")}

        def kv_get(key, default, attempts=3):
            return copy.deepcopy(json.loads(json.dumps(self.store[key]))) if key in self.store else default

        def kv_put(key, value, attempts=3):
            if key == extract.KV_STATE_KEY:
                self.state_writes[0] += 1
            self.store[key] = json.loads(json.dumps(value))  # round-trip: catches non-serialisable state

        extract.kv_get, extract.kv_put = kv_get, kv_put
        extract._send_telegram_message = lambda text, parse_mode=None, reply_markup=None: (self.sent.append(text) or len(self.sent))
        extract._edit_telegram_message = lambda *a, **k: True
        extract.send_url_batch = lambda urls, header=None: (self.batches.append(list(urls)) or True)
        extract.jitter_sleep = extract.safe_sleep = extract.batch_rest_sleep = lambda *a, **k: None
        extract.VALIDATE_URLS = False
        extract.PERSIST_INTERVAL_SECONDS = 0

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(extract, n, v)

    def run_with(self, client):
        extract.TelegramClient = lambda *a, **k: client
        extract.run()

    def state(self):
        return self.store[extract.KV_STATE_KEY]

    def urls(self):
        return self.store[extract.KV_URLS_KEY]["groups"]


class TestResilience(Base):
    def dialogs(self):
        return [dialog(-1, "A"), dialog(-2, "B"), dialog(-3, "C")]

    def test_inaccessible_group_is_skipped_not_fatal(self):
        c = FakeClient(self.dialogs(), {
            -1: [msg(1, "https://t.me/aaaa1")],
            -2: ChannelPrivateError(request=None),
            -3: [msg(1, "https://t.me/cccc3")],
        })
        self.run_with(c)  # must NOT raise
        self.assertIn("-1", self.urls())
        self.assertIn("-3", self.urls(), "group after the bad one must still be scanned")
        bad = self.state()["inaccessible_groups"]["-2"]
        self.assertEqual(bad["error"], "ChannelPrivateError")
        self.assertIn("-2", self.state()["last_scanned_at"])
        self.assertTrue(any("Inaccessible" in t for t in self.sent), self.sent)

    def test_cooling_group_skipped_then_retried_after_window(self):
        beh = {-1: [], -2: ChannelPrivateError(request=None), -3: []}
        self.run_with(FakeClient(self.dialogs(), beh))
        c2 = FakeClient(self.dialogs(), beh)
        self.run_with(c2)
        self.assertNotIn(-2, c2.calls, "still inside retry window -> not touched")
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.store[extract.KV_STATE_KEY]["inaccessible_groups"]["-2"]["at"] = old
        c3 = FakeClient(self.dialogs(), beh)
        self.run_with(c3)
        self.assertIn(-2, c3.calls, "retry window elapsed -> tried again")

    def test_vanished_group_is_forgotten(self):
        beh = {-1: [], -2: ChannelPrivateError(request=None), -3: []}
        self.run_with(FakeClient(self.dialogs(), beh))
        self.run_with(FakeClient([dialog(-1, "A"), dialog(-3, "C")], beh))  # B left the dialog list
        self.assertNotIn("-2", self.state()["inaccessible_groups"])

    def test_unexpected_error_saves_progress_and_names_group(self):
        c = FakeClient(self.dialogs(), {
            -1: [msg(1, "https://t.me/aaaa1")], -2: RuntimeError("boom"), -3: [],
        })
        with self.assertRaises(RuntimeError):
            self.run_with(c)
        self.assertIn("-1", self.urls(), "progress before the crash must be persisted")
        self.assertEqual(self.batches, [["https://t.me/aaaa1"]], "pending batch must be flushed to the bot")
        self.assertEqual(extract.FAIL_CONTEXT.get("current_group"), "B")

    def test_auth_key_duplicated_saves_progress_and_hints(self):
        exc = AuthKeyDuplicatedError(request=None)
        c = FakeClient(self.dialogs(), {-1: [msg(1, "https://t.me/aaaa1")], -2: exc, -3: []})
        with self.assertRaises(AuthKeyDuplicatedError):
            self.run_with(c)
        self.assertIn("-1", self.urls())
        hint = extract._fatal_hint(exc)
        self.assertIn("generate_session.py", hint)
        self.assertIn("TEST_STRING_SESSION", hint)
        self.assertEqual(extract._fatal_hint(RuntimeError("x")), "")

    def test_periodic_checkpoint(self):
        beh = {-1: [], -2: [], -3: []}
        extract.PERSIST_INTERVAL_SECONDS = 0
        self.run_with(FakeClient(self.dialogs(), beh))
        self.assertEqual(self.state_writes[0], 1, "disabled -> only the end-of-run persist")
        self.state_writes[0] = 0
        extract.PERSIST_INTERVAL_SECONDS = 1
        self.run_with(FakeClient(self.dialogs(), beh, slow=[-2]))
        self.assertGreaterEqual(self.state_writes[0], 2, "periodic checkpoint + end-of-run persist")


if __name__ == "__main__":
    unittest.main()
