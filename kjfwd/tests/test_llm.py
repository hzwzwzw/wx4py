import io
import json
import unittest

from kjfwd.kjfwd_bot.config import LLMConfig
from kjfwd.kjfwd_bot.llm import OpenAIChatClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class LLMTests(unittest.TestCase):
    def test_openai_chat_completions_request(self):
        captured = {}

        def urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"choices": [{"message": {"content": " OK "}}]})

        client = OpenAIChatClient(
            LLMConfig("https://example.test/v1", "test-model", "secret", timeout_seconds=12),
            urlopen=urlopen,
        )
        self.assertEqual("OK", client.complete("system", "user"))
        self.assertEqual("https://example.test/v1/chat/completions", captured["url"])
        self.assertEqual("test-model", captured["body"]["model"])
        self.assertEqual(12, captured["timeout"])

    def test_chat_sends_native_tools_and_tool_choice(self):
        captured = {}

        def urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        client = OpenAIChatClient(
            LLMConfig("https://example.test/v1", "test-model", "secret"),
            urlopen=urlopen,
        )
        tool = {"type": "function", "function": {"name": "web_search"}}
        message = client.chat(
            [{"role": "user", "content": "question"}],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "web_search"}},
            thinking=False,
        )
        self.assertEqual("call-1", message["tool_calls"][0]["id"])
        self.assertEqual([tool], captured["body"]["tools"])
        self.assertEqual("web_search", captured["body"]["tool_choice"]["function"]["name"])
        self.assertEqual({"type": "disabled"}, captured["body"]["thinking"])


if __name__ == "__main__":
    unittest.main()
