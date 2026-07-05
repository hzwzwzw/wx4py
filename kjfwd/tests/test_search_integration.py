import os
import unittest
from pathlib import Path

from kjfwd.kjfwd_bot.config import SearchConfig, load_dotenv
from kjfwd.kjfwd_bot.search import BraveSearchClient


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@unittest.skipUnless(
    os.getenv("KJFWD_RUN_SEARCH_TEST") == "1", "需要显式启用真实搜索测试"
)
class SearchIntegrationTests(unittest.TestCase):
    def test_real_brave_llm_context_endpoint(self):
        client = BraveSearchClient(
            SearchConfig(
                enabled=True,
                api_key=os.getenv("BRAVE_KEY", ""),
                retries=1,
                minimum_request_interval_seconds=0,
            )
        )
        result = client.search("Intel Core i5-12400 official core thread specifications")
        self.assertTrue(result["results"])
        self.assertTrue(result["results"][0]["url"])


if __name__ == "__main__":
    unittest.main()
