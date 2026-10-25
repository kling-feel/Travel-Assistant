"""Travel preparation advice stays in general chat, without a place search."""

import unittest
from datetime import date

from travel_assistant.services.dialogue_context import read_request
from tests.dialogue import test_p26a_dialogue as cases


class GeneralAdviceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cases.DialogueTests("runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_packing_advice_is_not_a_destination_request(self):
        intent, _, _ = read_request("请给我旅行中整理行李的建议", "general", {}, {},
                                    date(2026, 10, 25))
        self.assertEqual(intent, "general")

    def test_model_misclassification_still_answers_packing_question(self):
        model = cases.ScriptedModel({"intent": "recommendation", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            response = self.fixture.turn(client, sid, auth, "packing-advice-001",
                                         "请用两句话说说旅行中整理行李的建议。")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("行李", response.json()["answer"])
        self.assertNotIn("巴塞尔大教堂", response.json()["answer"])
        self.assertEqual(response.json()["references"], [])
        self.assertEqual(response.json()["actions"], [])


if __name__ == "__main__":
    unittest.main()
