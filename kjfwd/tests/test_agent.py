import json
import unittest

from kjfwd.kjfwd_bot.agent import (
    ToolCallingAgent,
    append_sources,
    response_needs_rewrite,
    sanitize_plain_text,
    should_require_search,
)
from kjfwd.kjfwd_bot.capabilities import ToolCapability


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, tools=None, tool_choice=None, thinking=None):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "thinking": thinking,
            }
        )
        return self.responses.pop(0)

    def complete(self, system_prompt, user_prompt, *, force_search=False):
        return "no tools"


class FakeTool(ToolCapability):
    def __init__(self):
        self.arguments = []

    @property
    def name(self):
        return "web_search"

    def definition(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def execute(self, arguments):
        self.arguments.append(arguments)
        return json.dumps({"results": [{"url": "https://example.test"}]})


class AgentTests(unittest.TestCase):
    def test_forced_search_uses_named_tool_then_returns_answer(self):
        client = FakeClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"hardware specs"}',
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "grounded answer"},
                {"role": "assistant", "content": "reviewed grounded answer"},
            ]
        )
        tool = FakeTool()
        agent = ToolCallingAgent(client, [tool])
        result = agent.complete("system", "question", force_search=True)
        self.assertIn("reviewed grounded answer", result)
        self.assertIn("https://example.test", result)
        self.assertEqual({"query": "hardware specs"}, tool.arguments[0])
        self.assertEqual(
            {"type": "function", "function": {"name": "web_search"}},
            client.calls[0]["tool_choice"],
        )
        self.assertFalse(client.calls[0]["thinking"])
        followup_messages = client.calls[1]["messages"]
        self.assertEqual("assistant", followup_messages[-2]["role"])
        self.assertEqual("tool", followup_messages[-1]["role"])
        self.assertEqual("call-1", followup_messages[-1]["tool_call_id"])

    def test_model_can_answer_without_search_in_auto_mode(self):
        client = FakeClient([{"role": "assistant", "content": "direct answer"}])
        agent = ToolCallingAgent(client, [FakeTool()])
        self.assertEqual("direct answer", agent.complete("system", "question"))
        self.assertEqual("auto", client.calls[0]["tool_choice"])

    def test_specific_hardware_parameters_require_search(self):
        self.assertTrue(
            should_require_search(
                "<current_request>请说明 Intel Core i5-12400 的核心数和线程数。</current_request>"
            )
        )
        self.assertFalse(
            should_require_search("<current_request>电脑突然开不了机</current_request>")
        )
        self.assertTrue(
            should_require_search("<current_request>Intel N100 是哪一年发布的？</current_request>")
        )

    def test_sources_are_appended_as_plain_text(self):
        result = append_sources(
            "结论。\n\n参考来源：\n- 模型自己写的来源",
            [{"title": "Official page", "url": "https://example.test/spec"}],
        )
        self.assertNotIn("模型自己写的来源", result)
        self.assertIn("Official page：https://example.test/spec", result)

    def test_plain_text_cleanup_removes_banned_filler_and_markdown(self):
        draft = "你好！先别着急，我来帮你一步步排查。\n- **先检查电源**"
        self.assertTrue(response_needs_rewrite(draft, 700))
        cleaned = sanitize_plain_text(draft)
        self.assertNotIn("先别着急", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("- ", cleaned)


if __name__ == "__main__":
    unittest.main()
