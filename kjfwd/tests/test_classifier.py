import unittest

from kjfwd.kjfwd_bot.classifier import KeywordQuestionClassifier, LLMQuestionClassifier


class FakeClassifierClient:
    def __init__(self, content='{"should_reply": false}'):
        self.content = content
        self.calls = []

    def chat(self, messages, *, thinking=False):
        self.calls.append((messages, thinking))
        return {"content": self.content}


class ClassifierTests(unittest.TestCase):
    def test_keyword_classifier_recognizes_short_followup_questions(self):
        classifier = KeywordQuestionClassifier()
        self.assertTrue(classifier.should_reply(group_name="群", content="还是不行"))
        self.assertTrue(classifier.should_reply(group_name="群", content="这是什么意思"))
        self.assertTrue(classifier.should_reply(group_name="群", content="啥意思"))
        self.assertFalse(classifier.should_reply(group_name="群", content="谢谢"))

    def test_keyword_classifier_ignores_obvious_staff_guidance(self):
        classifier = KeywordQuestionClassifier()
        self.assertFalse(classifier.should_reply(group_name="群", content="你先重启一下看看"))
        self.assertFalse(classifier.should_reply(group_name="群", content="把报错截图发一下"))
        self.assertFalse(classifier.should_reply(group_name="群", content="建议先备份数据再处理"))
        self.assertFalse(classifier.should_reply(group_name="群", content="可以先打开设备管理器看一下驱动"))

    def test_llm_classifier_prompt_prioritizes_customer_questions(self):
        client = FakeClassifierClient()
        classifier = LLMQuestionClassifier(client)
        self.assertFalse(classifier.should_reply(group_name="群", content="把报错截图发一下"))
        messages, thinking = client.calls[0]
        system_prompt = messages[0]["content"]
        self.assertFalse(thinking)
        self.assertIn("只应该回答像客户发出的求助消息", system_prompt)
        self.assertIn("科服队员", system_prompt)
        self.assertIn("宁可输出 false", system_prompt)


if __name__ == "__main__":
    unittest.main()
