import os
import unittest
from pathlib import Path

from kjfwd.kjfwd_bot.agent import ToolCallingAgent
from kjfwd.kjfwd_bot.config import LLMConfig, SearchConfig, load_dotenv
from kjfwd.kjfwd_bot.llm import OpenAIChatClient
from kjfwd.kjfwd_bot.search import BraveSearchClient, WebSearchTool


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


class CountingWebSearchTool(WebSearchTool):
    def __init__(self, client):
        super().__init__(client)
        self.call_count = 0

    def execute(self, arguments):
        self.call_count += 1
        return super().execute(arguments)


@unittest.skipUnless(
    os.getenv("KJFWD_RUN_AGENT_TEST") == "1", "需要显式启用真实 Agent 测试"
)
class AgentIntegrationTests(unittest.TestCase):
    def _build_agent(self):
        llm = OpenAIChatClient(
            LLMConfig(
                base_url=os.getenv("BASE_URL", ""),
                model=os.getenv("MODEL", ""),
                api_key=os.getenv("API_KEY", ""),
                temperature=0,
                max_tokens=700,
                timeout_seconds=60,
                retries=1,
            )
        )
        search = BraveSearchClient(
            SearchConfig(
                enabled=True,
                api_key=os.getenv("BRAVE_KEY", ""),
                retries=1,
                minimum_request_interval_seconds=0,
            )
        )
        tool = CountingWebSearchTool(search)
        return ToolCallingAgent(llm, [tool], max_tool_rounds=2), tool

    def test_real_forced_search_tool_call_loop(self):
        agent, tool = self._build_agent()
        answer = agent.complete(
            "使用工具核对硬件参数，并根据工具结果简短回答。",
            "Intel Core i5-12400 有多少核心和线程？",
            force_search=True,
        )
        self.assertTrue(answer.strip())
        self.assertGreaterEqual(tool.call_count, 1)

    def test_real_model_proactively_searches_hardware_parameters(self):
        agent, tool = self._build_agent()
        system_prompt = (ROOT / "kjfwd" / "prompts" / "system.md").read_text(encoding="utf-8")
        answer = agent.complete(
            system_prompt,
            "请说明 Intel Core i5-12400 的核心数和线程数。",
        )
        self.assertTrue(answer.strip())
        self.assertGreaterEqual(tool.call_count, 1)


if __name__ == "__main__":
    unittest.main()
