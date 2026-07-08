import json
import tempfile
import unittest
from pathlib import Path

from kjfwd.kjfwd_bot.config import load_config


class ConfigTests(unittest.TestCase):
    def test_group_listen_modes_and_reply_groups_are_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "groups": [
                            {
                                "name": "答疑群",
                                "bot_nickname": "柯基服务队",
                                "listen_mode": "question_only",
                                "reply_groups": ["机器人参考群"],
                            },
                            {
                                "name": "全量群",
                                "bot_nickname": "柯基服务队",
                                "listen_mode": "all_messages",
                                "reply_groups": ["参考一", "参考二"],
                            },
                        ],
                        "llm": {"base_url": "https://example.com/v1", "model": "test"},
                        "search": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(
                config_path,
                environ={"API_KEY": "key"},
            )
            self.assertEqual(("答疑群", "全量群"), config.group_names)
            self.assertEqual("question_only", config.listen_modes["答疑群"])
            self.assertEqual("all_messages", config.listen_modes["全量群"])
            self.assertEqual(("机器人参考群",), config.reply_groups["答疑群"])
            self.assertEqual(("参考一", "参考二"), config.reply_groups["全量群"])

    def test_invalid_listen_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "groups": [
                            {
                                "name": "答疑群",
                                "bot_nickname": "柯基服务队",
                                "listen_mode": "unknown",
                            }
                        ],
                        "llm": {"base_url": "https://example.com/v1", "model": "test"},
                        "search": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(config_path, environ={"API_KEY": "key"})


if __name__ == "__main__":
    unittest.main()
