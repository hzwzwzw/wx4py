import tempfile
import unittest
from pathlib import Path

from kjfwd.kjfwd_bot.history import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.tempdir.name) / "history.db", idle_timeout_seconds=1800)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_session_continues_inside_30_minutes_and_splits_after_gap(self):
        first, _ = self.store.record_group_message("群", "一", 1000, "a")
        second, _ = self.store.record_group_message("群", "二", 2799, "b")
        third, _ = self.store.record_group_message("群", "三", 4600, "c")
        self.assertEqual(first.session_id, second.session_id)
        self.assertNotEqual(second.session_id, third.session_id)

    def test_source_key_and_trigger_claim_are_idempotent(self):
        message, inserted = self.store.record_group_message("群", "@bot hello", 1000, "uia:1.2")
        duplicate, inserted_again = self.store.record_group_message("群", "@bot hello", 1001, "uia:1.2")
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)
        self.assertEqual(message.id, duplicate.id)

        claim = self.store.claim_trigger("trigger", "fingerprint", "群", message.id, 1000, 5)
        duplicate_claim = self.store.claim_trigger("trigger", "fingerprint", "群", message.id, 1001, 5)
        self.assertTrue(claim.accepted)
        self.assertFalse(duplicate_claim.accepted)
        self.assertFalse(duplicate_claim.sent)

        reply = self.store.record_assistant_message("群", message.session_id, "answer", 1002)
        self.store.mark_trigger_sent(claim.trigger_id, reply.id)
        row = self.store.get_trigger(claim.trigger_id)
        self.assertEqual(1, row["sent"])
        self.assertEqual("sent", row["status"])

    def test_snapshot_is_frozen_and_cropped_from_oldest(self):
        messages = []
        for index in range(5):
            message, _ = self.store.record_group_message(
                "群", f"message-{index}", 1000 + index, f"key-{index}"
            )
            messages.append(message)
        snapshot = self.store.snapshot(messages[-1], max_messages=3, max_characters=100)
        self.assertEqual(["message-2", "message-3", "message-4"], [m.content for m in snapshot.messages])
        self.store.record_group_message("群", "future", 1006, "future")
        self.assertNotIn("future", [m.content for m in snapshot.messages])

    def test_conversation_snapshot_only_contains_bound_messages(self):
        first = self.store.create_conversation("群", title="打印机", now=1000)
        second = self.store.create_conversation("群", title="清灰", now=1001)
        m1, _ = self.store.record_group_message("群", "打印机脱机", 1002, "c1")
        m2, _ = self.store.record_group_message("群", "幻14清灰", 1003, "c2")
        m3, _ = self.store.record_group_message("群", "打印机还是不行", 1004, "c3")
        self.store.bind_message_to_conversation(m1.id, first.id, trigger_at=1002)
        self.store.bind_message_to_conversation(m2.id, second.id, trigger_at=1003)
        m3 = self.store.bind_message_to_conversation(m3.id, first.id, trigger_at=1004)

        snapshot = self.store.conversation_snapshot(m3, first.id, max_messages=10, max_characters=1000)
        self.assertEqual(["打印机脱机", "打印机还是不行"], [m.content for m in snapshot.messages])
        self.assertNotIn("幻14清灰", [m.content for m in snapshot.messages])

    def test_ambiguous_snapshot_uses_global_history(self):
        m1, _ = self.store.record_group_message("群", "打印机脱机", 1000, "a1")
        self.store.record_group_message("群", "幻14清灰", 1001, "a2")
        trigger, _ = self.store.record_group_message("群", "@bot 还是不行", 1002, "a3")
        ambiguous = self.store.create_conversation("群", title="未判定追问", now=1002, status="ambiguous")
        snapshot = self.store.ambiguous_snapshot(
            trigger,
            conversation_id=ambiguous.id,
            global_seconds=3600,
            global_max_messages=10,
            max_characters=1000,
        )
        self.assertTrue(snapshot.ambiguous)
        self.assertEqual(["打印机脱机", "幻14清灰", "@bot 还是不行"], [m.content for m in snapshot.global_messages])

    def test_conversation_operations_reject_cross_group_binding(self):
        first_group_conversation = self.store.create_conversation("一群", title="打印机", now=1000)
        other_message, _ = self.store.record_group_message("二群", "@bot 还是不行", 1001, "cross")

        with self.assertRaises(ValueError):
            self.store.bind_message_to_conversation(other_message.id, first_group_conversation.id, trigger_at=1001)

        with self.assertRaises(ValueError):
            self.store.record_assistant_message(
                "二群",
                other_message.session_id,
                "回复",
                1002,
                first_group_conversation.id,
            )

        with self.assertRaises(ValueError):
            self.store.conversation_snapshot(
                other_message,
                first_group_conversation.id,
                max_messages=10,
                max_characters=1000,
            )


if __name__ == "__main__":
    unittest.main()
