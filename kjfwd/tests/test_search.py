import json
import unittest

from kjfwd.kjfwd_bot.config import SearchConfig
from kjfwd.kjfwd_bot.search import (
    BraveSearchClient,
    WebSearchTool,
    prefer_official_query,
    sanitize_search_query,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SearchTests(unittest.TestCase):
    def test_brave_llm_context_request_and_cache(self):
        requests = []

        def urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(
                {
                    "grounding": {
                        "generic": [
                            {
                                "title": "Official specification",
                                "url": "https://example.test/spec",
                                "snippets": ["The device has two ports."],
                            }
                        ]
                    }
                }
            )

        config = SearchConfig(
            enabled=True,
            api_key="brave-secret",
            cache_seconds=900,
            minimum_request_interval_seconds=0,
        )
        client = BraveSearchClient(config, urlopen=urlopen)
        first = client.search("device model specification")
        second = client.search("device model specification")
        self.assertEqual(first, second)
        self.assertEqual(1, len(requests))
        request, timeout = requests[0]
        self.assertEqual(config.endpoint, request.full_url)
        self.assertEqual("brave-secret", request.headers["X-subscription-token"])
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(4096, body["maximum_number_of_tokens"])
        self.assertEqual("Official specification", first["results"][0]["title"])

    def test_search_tool_blocks_proxy_topics_without_network_call(self):
        class NeverCalledClient:
            def search(self, query):
                raise AssertionError("不应调用搜索 API")

        result = json.loads(WebSearchTool(NeverCalledClient()).execute({"query": "VPN 配置"}))
        self.assertIn("禁止", result["error"])

    def test_query_removes_basic_personal_information(self):
        cleaned = sanitize_search_query("联系 test@example.com 或 13812345678 查询硬件")
        self.assertNotIn("test@example.com", cleaned)
        self.assertNotIn("13812345678", cleaned)

    def test_search_query_prefers_official_sources(self):
        self.assertEqual("Intel N100 参数 官方", prefer_official_query("Intel N100 参数"))
        self.assertEqual(
            "site:intel.com Intel N100", prefer_official_query("site:intel.com Intel N100")
        )


if __name__ == "__main__":
    unittest.main()
