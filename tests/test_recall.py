import asyncio
import tempfile
import unittest
from pathlib import Path

import recall
from storage import Storage


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_message_id = 100

    async def call_action(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if action == "send_group_msg":
            self.next_message_id += 1
            return {"data": {"message_id": self.next_message_id}}
        return {"status": "ok"}


class _FakeEvent:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot

    def get_group_id(self) -> str:
        return "123"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_self_id(self) -> str:
        return "456"


class RecallTest(unittest.IsolatedAsyncioTestCase):
    async def test_slot_is_released_after_recall(self) -> None:
        original_builder = recall._build_component
        recall._build_component = lambda record: ("component", record.id)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                storage = Storage(root / "laizhi.db", root)
                try:
                    source = root / "tmp" / "asset"
                    source.write_bytes(b"asset")
                    record = storage.store_file(
                        source,
                        file_hash="b" * 64,
                        kind="image",
                        ext="png",
                        mime="image/png",
                        size=5,
                    )
                    bot = _FakeBot()
                    event = _FakeEvent(bot)
                    manager = recall.RecallManager(
                        storage,
                        recall_after_seconds=1,
                        max_outstanding=1,
                        cooldown_seconds=0,
                    )
                    await manager.initialize()
                    first = await manager.send_media(event, record)
                    second = await manager.send_media(event, record)
                    self.assertTrue(first.direct_sent)
                    self.assertTrue(second.throttled)
                    await asyncio.sleep(1.1)
                    self.assertEqual(manager.outstanding("123"), 0)
                    self.assertEqual(
                        [action for action, _ in bot.calls],
                        ["send_group_msg", "delete_msg"],
                    )
                    await manager.shutdown()
                finally:
                    storage.close()
        finally:
            recall._build_component = original_builder


if __name__ == "__main__":
    unittest.main()
