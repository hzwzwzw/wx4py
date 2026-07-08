import tempfile
import threading
import time
import unittest
from pathlib import Path

from wx4py import MessageEvent, ReplyAction

from kjfwd.kjfwd_bot.capabilities import CapabilityRegistry
from kjfwd.kjfwd_bot.handler import (
    KJFWDHandler,
    append_reference_notice,
    build_help_text,
    is_help_request,
    raw_control_mentions_bot,
)
from kjfwd.kjfwd_bot.history import HistoryStore
from kjfwd.kjfwd_bot.models import ConversationRoute
from kjfwd.kjfwd_bot.prompt import PromptBuilder


class FakeRaw:
    def __init__(self, runtime_id):
        self.runtime_id = runtime_id

    def GetRuntimeId(self):
        return self.runtime_id


class FakeControl:
    def __init__(self, name="", children=()):
        self.Name = name
        self.children = list(children)

    def GetChildren(self):
        return self.children


class FakeModel:
    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, user_prompt, *, force_search=False):
        self.calls.append((system_prompt, user_prompt, force_search))
        return "请先断电，再检查电源线。"


class FakeRouter:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def route(self, *, group_name, request, candidates, recent_messages):
        self.calls.append((group_name, request, candidates, recent_messages))
        if self.routes:
            return self.routes.pop(0)
        return ConversationRoute("create_new", title=request[:12] or "新会话")


class FakeClassifier:
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.calls = []

    def should_reply(self, *, group_name, content):
        self.calls.append((group_name, content))
        if self.decisions:
            return self.decisions.pop(0)
        return False


class BlockingModel:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, system_prompt, user_prompt, *, force_search=False):
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
            groups=("客户群", "二群"),
            bot_nicknames={"客户群": "柯基服务队", "二群": "柯基服务队"},
            history=self.store,
            model=self.model,
            prompt_builder=PromptBuilder(system, CapabilityRegistry([])),
            router=FakeRouter(),
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
        deadline = time.time() + 1
        row = self.store.get_trigger(1)
        while not row["sent"] and time.time() < deadline:
            time.sleep(0.01)
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

    def test_all_messages_mode_replies_without_at(self):
        self.handler.listen_modes["客户群"] = "all_messages"
        event = MessageEvent(
            group="客户群",
            content="电脑开不了机",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=False,
            raw=FakeRaw((31, 1)),
        )
        self.handler.handle(event)
        self.assertTrue(self.action_ready.wait(2))
        self.assertEqual(1, len(self.model.calls))
        self.assertIn("<current_request>\n电脑开不了机", self.model.calls[0][1])

    def test_question_only_mode_uses_classifier_before_replying(self):
        classifier = FakeClassifier([False, True])
        self.handler.classifier = classifier
        self.handler.listen_modes["客户群"] = "question_only"

        self.handler.handle(
            MessageEvent("客户群", "谢谢", time.time(), "柯基服务队", False, FakeRaw((32, 1)))
        )
        time.sleep(0.05)
        self.assertEqual([], self.model.calls)

        self.handler.handle(
            MessageEvent("客户群", "电脑蓝屏怎么办", time.time() + 1, "柯基服务队", False, FakeRaw((32, 2)))
        )
        self.assertTrue(self.action_ready.wait(2))
        self.assertEqual(2, len(classifier.calls))
        self.assertEqual(1, len(self.model.calls))
        self.assertIn("电脑蓝屏怎么办", self.model.calls[0][1])

    def test_split_reply_groups_emit_to_configured_robot_group(self):
        self.handler.reply_groups["客户群"] = ("机器人参考群",)
        self.handler.handle(
            MessageEvent(
                "客户群",
                "@柯基服务队\u2005 电脑蓝屏",
                time.time(),
                "柯基服务队",
                True,
                FakeRaw((33, 1)),
            )
        )
        self.assertTrue(self.action_ready.wait(2))
        self.assertEqual("机器人参考群", self.actions[0].group)
        self.assertIn("[来源群：客户群]", self.actions[0].content)
        self.assertIn("请先断电，再检查电源线。", self.actions[0].content)

    def test_inline_at_in_raw_child_controls_triggers_reply(self):
        raw = FakeControl(
            "普通消息文本",
            [FakeControl("前文 "), FakeControl("@柯基服务队\u2005"), FakeControl(" 后文")],
        )
        event = MessageEvent(
            group="客户群",
            content="前文 后文",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=False,
            raw=raw,
        )
        self.handler.handle(event)
        self.assertTrue(self.action_ready.wait(2))
        self.assertEqual(1, len(self.model.calls))

    def test_inline_at_can_be_split_across_adjacent_controls(self):
        raw = FakeControl("", [FakeControl("前文@"), FakeControl("柯基服务队"), FakeControl("后文")])
        self.assertTrue(raw_control_mentions_bot(raw, "柯基服务队"))

    def test_raw_fallback_does_not_match_nickname_without_at_sign(self):
        raw = FakeControl("", [FakeControl("柯基服务队"), FakeControl("普通讨论")])
        self.assertFalse(raw_control_mentions_bot(raw, "柯基服务队"))

    def test_raw_fallback_respects_depth_limit(self):
        raw = FakeControl("", [FakeControl("", [FakeControl("", [FakeControl("@柯基服务队")])])])
        self.assertFalse(raw_control_mentions_bot(raw, "柯基服务队", max_depth=2))

    def test_help_command_returns_introduction_without_model_call(self):
        event = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 /help",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((12, 1)),
        )
        self.handler.handle(event)
        self.assertTrue(self.action_ready.wait(1))
        self.assertEqual([], self.model.calls)
        self.assertIn("柯基服务队群聊答疑助手", self.actions[0].content)
        self.assertIn("/new [新问题]", self.actions[0].content)
        self.assertIn("/search <问题>", self.actions[0].content)

    def test_help_command_survives_wechat_suffix_after_bot_mention(self):
        event = MessageEvent(
            group="客户群",
            content="@柯基服务队@微信 /help",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((12, 3)),
        )
        self.handler.handle(event)
        self.assertTrue(self.action_ready.wait(1))
        self.assertEqual([], self.model.calls)
        self.assertIn("可用指令", self.actions[0].content)

    def test_command_is_not_executed_from_arbitrary_message_position(self):
        event = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 请不要执行 /clear",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((12, 4)),
        )
        self.handler.handle(event)
        deadline = time.time() + 2
        while not self.model.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(self.model.calls))
        self.assertIn("请不要执行 /clear", self.model.calls[0][1])

    def test_natural_help_question_returns_help(self):
        event = MessageEvent(
            group="客户群",
            content="@柯基服务队\u2005 如何使用你？",
            timestamp=time.time(),
            group_nickname="柯基服务队",
            is_at_me=True,
            raw=FakeRaw((12, 2)),
        )
        self.handler.handle(event)
        self.assertTrue(self.action_ready.wait(1))
        self.assertEqual([], self.model.calls)
        self.assertIn("可用指令", self.actions[0].content)
        self.assertTrue(is_help_request("你有哪些指令？"))
        self.assertTrue(is_help_request("怎么使用这个 agent？"))

    def test_help_detection_does_not_capture_a_normal_how_to_question(self):
        self.assertFalse(is_help_request("如何使用你推荐的系统修复命令？"))

    def test_help_text_lists_loaded_skills(self):
        text = build_help_text(
            (("explain", "细化操作指南"), ("repair-risk", "维修服务风险判断"))
        )
        self.assertIn("/explain <问题>", text)
        self.assertIn("/repair-risk <问题>：维修服务风险判断", text)

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

    def test_new_alias_with_following_text_starts_new_request(self):
        now = time.time()
        self.handler.handle(
            MessageEvent("客户群", "应被忽略的旧内容", now, "柯基服务队", False, FakeRaw((10, 1)))
        )
        self.handler.handle(
            MessageEvent(
                "客户群",
                "@柯基服务队\u2005 /new 新会话的问题",
                now + 1,
                "柯基服务队",
                True,
                FakeRaw((10, 2)),
            )
        )
        deadline = time.time() + 2
        while not self.model.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(self.model.calls))
        user_prompt = self.model.calls[0][1]
        self.assertNotIn("应被忽略的旧内容", user_prompt)
        self.assertIn("<current_request>\n新会话的问题", user_prompt)

    def test_search_command_forces_search_and_is_removed_from_request(self):
        now = time.time()
        self.handler.handle(
            MessageEvent(
                "客户群",
                "@柯基服务队\u2005 /search 某硬件型号参数",
                now,
                "柯基服务队",
                True,
                FakeRaw((11, 1)),
            )
        )
        deadline = time.time() + 2
        while not self.model.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(self.model.calls))
        _, user_prompt, force_search = self.model.calls[0]
        self.assertTrue(force_search)
        self.assertIn("<current_request>\n某硬件型号参数", user_prompt)
        self.assertNotIn("<current_request>\n/search", user_prompt)

    def test_router_can_route_trigger_to_existing_conversation(self):
        now = time.time()
        first = MessageEvent(
            "客户群", "@柯基服务队\u2005 打印机脱机", now, "柯基服务队", True, FakeRaw((21, 1))
        )
        self.handler.handle(first)
        deadline = time.time() + 2
        while len(self.actions) < 1 and time.time() < deadline:
            time.sleep(0.01)
        first_conv = self.store.get_trigger(1)["reply_message_id"]
        conversations = self.store.list_active_conversations("客户群", now=now + 1)
        self.assertEqual(1, len(conversations))
        conversation_id = conversations[0].id

        self.handler.router = FakeRouter(
            [ConversationRoute("use_existing", conversation_id=conversation_id)]
        )
        second = MessageEvent(
            "客户群", "@柯基服务队\u2005 还是打印不了", now + 2, "柯基服务队", True, FakeRaw((21, 2))
        )
        self.handler.handle(second)
        deadline = time.time() + 2
        while len(self.actions) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(2, len(self.model.calls))
        self.assertIn("打印机脱机", self.model.calls[1][1])
        self.assertIn("还是打印不了", self.model.calls[1][1])
        self.assertIn(f"[conv: {conversation_id[:8]}]", self.actions[1].content)

    def test_ambiguous_route_uses_global_history_without_polluting_existing_conversations(self):
        now = time.time()
        first = self.store.create_conversation("客户群", title="打印机脱机", now=now)
        second = self.store.create_conversation("客户群", title="幻14清灰", now=now + 1)
        self.store.record_group_message("客户群", "打印机之前显示脱机", now + 2, "g1")
        self.store.record_assistant_message("客户群", "s", "取消脱机使用打印机。", now + 3, first.id)
        self.store.record_group_message("客户群", "幻14可能涉及液金", now + 4, "g2")
        self.store.record_assistant_message("客户群", "s", "先确认具体年份。", now + 5, second.id)

        self.handler.router = FakeRouter([ConversationRoute("ambiguous", reason="too short")])
        self.handler.handle(
            MessageEvent("客户群", "@柯基服务队\u2005 还是不行", now + 6, "柯基服务队", True, FakeRaw((22, 1)))
        )
        deadline = time.time() + 2
        while len(self.actions) < 1 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(1, len(self.model.calls))
        user_prompt = self.model.calls[0][1]
        self.assertIn("<global_recent_transcript>", user_prompt)
        self.assertIn("打印机之前显示脱机", user_prompt)
        self.assertIn("幻14可能涉及液金", user_prompt)
        self.assertIn("[conv: ambiguous]", self.actions[0].content)
        active = self.store.list_active_conversations("客户群", now=now + 7)
        self.assertEqual({first.id, second.id}, {item.id for item in active})

    def test_low_information_followup_does_not_cross_group_boundaries(self):
        now = time.time()
        self.handler.handle(
            MessageEvent(
                "客户群",
                "@柯基服务队\u2005 打印机脱机",
                now,
                "柯基服务队",
                True,
                FakeRaw((23, 1)),
            )
        )
        deadline = time.time() + 2
        while len(self.actions) < 1 and time.time() < deadline:
            time.sleep(0.01)

        self.handler.handle(
            MessageEvent(
                "二群",
                "@柯基服务队\u2005 还是不行",
                now + 1,
                "柯基服务队",
                True,
                FakeRaw((23, 2)),
            )
        )
        deadline = time.time() + 2
        while len(self.actions) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(2, len(self.model.calls))
        second_prompt = self.model.calls[1][1]
        self.assertIn("还是不行", second_prompt)
        self.assertNotIn("打印机脱机", second_prompt)
        first_group = self.store.list_active_conversations("客户群", now=now + 2)
        second_group = self.store.list_active_conversations("二群", now=now + 2)
        self.assertEqual(1, len(first_group))
        self.assertEqual(1, len(second_group))
        self.assertNotEqual(first_group[0].id, second_group[0].id)

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
