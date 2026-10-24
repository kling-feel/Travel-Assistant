"""Keep policy questions separate from card withdrawal commands."""

import unittest

from travel_assistant.services.dialogue_boundaries import boundary_intent


class CardBoundaryTests(unittest.TestCase):
    def test_policy_question_with_card_noun_does_not_withdraw(self):
        message = "请说明机票改签与取消的模拟规则，不要生成交易卡。"
        self.assertIsNone(boundary_intent(message))

    def test_explicit_card_cancellation_still_withdraws(self):
        self.assertEqual(boundary_intent("取消刚才的卡"), "withdraw")
        self.assertEqual(boundary_intent("把待确认卡取消"), "withdraw")


if __name__ == "__main__":
    unittest.main()
