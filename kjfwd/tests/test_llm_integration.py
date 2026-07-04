import os
import unittest
from pathlib import Path

from kjfwd.kjfwd_bot.config import LLMConfig, load_dotenv
from kjfwd.kjfwd_bot.llm import OpenAIChatClient


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@unittest.skipUnless(os.getenv("KJFWD_RUN_LLM_TEST") == "1", "需要显式启用真实 LLM 测试")
class LLMIntegrationTests(unittest.TestCase):
    def test_real_openai_compatible_endpoint(self):
        base_url = os.getenv("BASE_URL") or ""
        client = OpenAIChatClient(
            LLMConfig(
                base_url=base_url,
                model=os.getenv("MODEL", ""),
                api_key=os.getenv("API_KEY", ""),
                temperature=0,
                # 推理模型可能先消耗一部分输出额度；过小会得到空 content。
                max_tokens=700,
                timeout_seconds=60,
                retries=1,
            )
        )
        result = client.complete("你只进行接口连通性测试。", "请只回复：OK")
        self.assertTrue(result.strip())


if __name__ == "__main__":
    unittest.main()
