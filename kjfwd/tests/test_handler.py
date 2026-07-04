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


if __name__ == "__main__":
    unittest.main()
