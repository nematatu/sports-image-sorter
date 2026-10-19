import unittest

from sports_sorter.clip_engine import NEGATIVES, PROMPTS, decide


class ClipPolicyTests(unittest.TestCase):
    def scores(self):
        return [0.1] * (len(PROMPTS) + len(NEGATIVES))

    def test_close_sports_are_unclassified(self):
        scores = self.scores()
        scores[0], scores[1] = 0.32, 0.31
        self.assertEqual(decide(scores).label, "未分類")

    def test_portrait_beats_sport(self):
        scores = self.scores()
        scores[0], scores[len(PROMPTS)] = 0.32, 0.36
        self.assertEqual(decide(scores).label, "未分類")

    def test_clear_candidate_and_threshold(self):
        scores = self.scores()
        scores[0] = 0.36
        self.assertEqual(decide(scores).label, "バドミントン")
        scores[0] = 0.25
        self.assertEqual(decide(scores).label, "未分類")


if __name__ == "__main__":
    unittest.main()
