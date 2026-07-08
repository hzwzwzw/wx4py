import unittest

from kjfwd.kjfwd_bot.classifier import KeywordQuestionClassifier


class ClassifierTests(unittest.TestCase):
    def test_keyword_classifier_recognizes_short_followup_questions(self):
        classifier = KeywordQuestionClassifier()
        self.assertTrue(classifier.should_reply(group_name="群", content="还是不行"))
        self.assertTrue(classifier.should_reply(group_name="群", content="这是什么意思"))
        self.assertTrue(classifier.should_reply(group_name="群", content="啥意思"))
        self.assertFalse(classifier.should_reply(group_name="群", content="谢谢"))


if __name__ == "__main__":
    unittest.main()
