import tempfile
import threading
import time
import unittest
from pathlib import Path

from wx4py import MessageEvent, ReplyAction

from kjfwd.kjfwd_bot.capabilities import CapabilityRegistry
from kjfwd.kjfwd_bot.handler import KJFWDHandler, append_reference_notice
from kjfwd.kjfwd_bot.history import HistoryStore
from kjfwd.kjfwd_bot.prompt import PromptBuilder


class FakeRaw:
    def __init__(self, runtime_id):
        self.runtime_id = runtime_id

    def GetRuntimeId(self):
        return self.runtime_id


class FakeModel:
    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return "请先断电，再检查电源线。"


class BlockingModel:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, system_prompt, user_prompt):
        self.started.set()
        self.release.wait(2)
        return "这是一条已经过期的回复。"


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        system = root / "system.md"
        system.write_text("SYSTEM", encoding="utf-8")
        self.store = HistoryStore(root / "history.db")
        self.model = FakeModel()
        self.handler = KJFWDHandler(
            groups=("客户群",),
            bot_nicknames={"客户群": "柯基服务队"},
            history=self.store,
            model=self.model,
            prompt_builder=PromptBuilder(system, CapabilityRegistry([])),
        )
        self.actions = []
        self.action_ready = threading.Event()

        def emit(action):
            self.actions.append(action)
            self.action_ready.set()

        self.handler.set_action_emitter(emit)

    def tearDown(self):
        self.handler.stop()
        self.store.close()
        self.tempdir.cleanup()

    def test_duplicate_at_with_changed_runtime_id_only_generates_one_reply_and_sets_sent(self):
        event = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 电脑开不了机",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((1, 2, 3)),
        )
        self.handler.handle(event)
        duplicate = MessageEvent(
            group=event.group,
            content=event.content,
            timestamp=event.timestamp + 0.1,
            group_nickname=event.group_nickname,
            is_at_me=True,
            raw=FakeRaw((4, 5, 6)),
        )
        self.handler.handle(duplicate)
        self.assertTrue(self.action_ready.wait(2))
        self.assertEqual(1, len(self.model.calls))
        self.assertEqual(1, len(self.actions))
        self.assertIsInstance(self.actions[0], ReplyAction)
        self.assertTrue(self.actions[0].content.endswith("（内容仅供参考）"))
        row = self.store.get_trigger(1)
        self.assertEqual(1, row["sent"])

    def test_reused_runtime_id_with_new_content_is_not_treated_as_duplicate(self):
        now = time.time()
        first = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 第一个问题",
            timestamp=now,
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((1, 2, 3)),
        )
        second = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 这是新的问题",
            timestamp=now + 0.1,
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((1, 2, 3)),
        )
        self.handler.handle(first)
        self.handler.handle(second)
        deadline = time.time() + 2
        while len(self.actions) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual(2, len(self.actions))

    def test_reference_notice_is_not_duplicated(self):
        text = "操作建议。\n（内容仅供参考）"
        self.assertEqual(text, append_reference_notice(text))

    def test_normal_message_is_stored_without_model_call(self):
        event = MessageEvent(
            group="客户群",
            content="前面有人讨论过电源问题",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=False,
            raw=FakeRaw((9, 9, 9)),
        )
        self.handler.handle(event)
        time.sleep(0.05)
        self.assertEqual([], self.model.calls)

    def test_clear_starts_new_context_without_calling_model(self):
        now = time.time()
        self.handler.handle(
            MessageEvent("客户群", "旧对话内容", now, "柯基服务队", False, FakeRaw((7, 1)))
        )
        self.handler.handle(
            MessageEvent(
                "客户群", "@柯基服务队\u2005 /clear", now + 1, "柯基服务队", True, FakeRaw((7, 2))
            )
        )
        self.assertTrue(self.action_ready.wait(1))
        self.assertEqual([], self.model.calls)
        self.assertIn("已清除此前的聊天上下文", self.actions[0].content)

        self.handler.handle(
            MessageEvent(
                "客户群", "@柯基服务队\u2005 新问题", now + 2, "柯基服务队", True, FakeRaw((7, 3))
            )
        )
        deadline = time.time() + 2
        while not self.model.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(self.model.calls))
        user_prompt = self.model.calls[0][1]
        self.assertNotIn("旧对话内容", user_prompt)
        self.assertIn("新问题", user_prompt)

    def test_clear_with_following_text_uses_it_as_first_new_request(self):
        now = time.time()
        self.handler.handle(
            MessageEvent("客户群", "旧信息", now, "柯基服务队", False, FakeRaw((8, 1)))
        )
        self.handler.handle(
            MessageEvent(
                "客户群",
                "@柯基服务队\u2005 /clear 如何修复系统？",
                now + 1,
                "柯基服务队",
                True,
                FakeRaw((8, 2)),
            )
        )
        deadline = time.time() + 2
        while not self.model.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(self.model.calls))
        user_prompt = self.model.calls[0][1]
        self.assertNotIn("旧信息", user_prompt)
        self.assertIn("<current_request>\n如何修复系统？", user_prompt)

    def test_clear_discards_an_inflight_old_reply(self):
        blocking_model = BlockingModel()
        self.handler.model = blocking_model
        now = time.time()
        self.handler.handle(
            MessageEvent(
                "客户群", "@柯基服务队\u2005 旧问题", now, "柯基服务队", True, FakeRaw((9, 1))
            )
        )
        self.assertTrue(blocking_model.started.wait(1))
        self.handler.handle(
            MessageEvent(
                "客户群", "@柯基服务队\u2005 /clear", now + 1, "柯基服务队", True, FakeRaw((9, 2))
            )
        )
        blocking_model.release.set()
        deadline = time.time() + 2
        while self.store.get_trigger(1)["status"] == "pending" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual("cleared", self.store.get_trigger(1)["error"])
        self.assertEqual(1, len(self.actions))
        self.assertIn("已清除此前的聊天上下文", self.actions[0].content)


if __name__ == "__main__":
    unittest.main()
