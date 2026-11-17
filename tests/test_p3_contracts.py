import unittest

import model_tasks
import product_policy


class PolicyTests(unittest.TestCase):
    def test_ready_is_not_human_verified(self):
        result = product_policy.evaluate({}, execution="completed", document_sync="synced")
        self.assertEqual(result["deliverable"], "ready_for_request")
        self.assertEqual(result["human_verification"], "none")
        self.assertIn("результат не подтверждён человеком", result["limits"])

    def test_required_outputs_block(self):
        result = product_policy.evaluate(
            {"require_conspect": True, "require_named_speakers": True},
            execution="waiting_model", document_sync="pending", unnamed_speakers=1)
        self.assertEqual(result["deliverable"], "blocked")
        self.assertEqual(len(result["blockers"]), 4)

    def test_bad_policy_is_rejected(self):
        for policy in ({"unknown": True}, {"question_budget": True}, {"require_conspect": 1}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                product_policy.resolve(policy)

    def test_roles_do_not_automatically_change_quality_requirements(self):
        for role in ("simple", "advanced", "developer"):
            result = product_policy.evaluate(
                {"participation": role, "require_named_speakers": True},
                execution="completed", document_sync="synced", unnamed_speakers=1)
            self.assertEqual(result["deliverable"], "blocked")

    def test_exact_human_checked_result_does_not_become_ready_on_asr_completion(self):
        result = product_policy.evaluate(
            {"require_human_verification": True}, execution="completed", document_sync="synced")
        self.assertEqual(result["deliverable"], "blocked")

    def test_fully_confirmed_result_still_reports_uncertain_places(self):
        result = product_policy.evaluate(
            {}, execution="completed", document_sync="synced",
            human_verification="confirmed", unresolved=1)
        self.assertEqual(result["deliverable"], "usable_with_limits")


class ModelTaskTests(unittest.TestCase):
    def setUp(self):
        self.task = model_tasks.build("recording", 1, [{"word_id": "w1", "text": "текст"}])
        self.answer = {"source_revision": 1, "sections": [{"text": "Кратко", "evidence": ["w1"]}]}

    def test_valid_response_stays_model_authored(self):
        result = model_tasks.validate(self.task, self.answer, current_revision=1)
        self.assertEqual(result["actor_kind"], "model")
        self.assertEqual(result["human_verification"], "none")

    def test_stale_and_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):
            model_tasks.validate(self.task, self.answer, current_revision=2)
        self.answer["sections"][0]["evidence"] = ["missing"]
        with self.assertRaises(ValueError):
            model_tasks.validate(self.task, self.answer, current_revision=1)

    def test_boolean_revision_is_not_an_integer_revision(self):
        self.answer["source_revision"] = True
        with self.assertRaises(ValueError):
            model_tasks.validate(self.task, self.answer, current_revision=1)

    def test_unknown_fields_are_not_exposed(self):
        with self.assertRaises(ValueError):
            model_tasks.build("recording", 1, [{"word_id": "w1", "text": "x", "path": "private"}])
        with self.assertRaises(ValueError):
            model_tasks.build("recording", 1, [{"word_id": "w1", "text": None}])


if __name__ == "__main__":
    unittest.main()
