"""Unit tests for R1–R7 rule evaluation on delivery fixtures."""

from __future__ import annotations

import json
import unittest

from filter_traj_multi_plat import RULES, compute_session_id, evaluate, extract
from tests.paths import FIXTURES_DATA
from trajery.parser import unwrap_delivery_record


def _load_fixture(name: str) -> dict:
    line = (FIXTURES_DATA / name).read_text(encoding="utf-8").strip().splitlines()[0]
    return json.loads(line)


class RuleEvaluationTests(unittest.TestCase):
    def test_passing_fixture_passes_all_rules(self) -> None:
        record = _load_fixture("delivery_pass.jsonl")
        unwrapped = unwrap_delivery_record(record)
        self.assertIsNotNone(unwrapped)
        trajectory = extract(unwrapped)
        passed, reasons = evaluate(trajectory)
        self.assertTrue(passed, reasons)
        self.assertEqual(reasons, [])

    def test_each_drop_fixture_fails_expected_rule(self) -> None:
        cases = {
            "delivery_drop_wrong_model.jsonl": "model_not_target_family",
            "delivery_drop_no_tools.jsonl": "no_tools",
            "delivery_drop_short.jsonl": "messages_le_5",
            "delivery_drop_no_tool_call.jsonl": "no_assistant_tool_call",
            "delivery_drop_tool_calls.jsonl": "stop_reason_tool_calls",
            "delivery_drop_no_system.jsonl": "no_system_prompt",
            "delivery_drop_heartbeat.jsonl": "heartbeat_non_task_traffic",
        }
        for fixture_name, expected_reason in cases.items():
            with self.subTest(fixture=fixture_name, reason=expected_reason):
                record = _load_fixture(fixture_name)
                unwrapped = unwrap_delivery_record(record)
                self.assertIsNotNone(unwrapped)
                trajectory = extract(unwrapped)
                passed, reasons = evaluate(trajectory)
                self.assertFalse(passed)
                self.assertIn(expected_reason, reasons)

    def test_all_rules_are_evaluated_even_after_first_failure(self) -> None:
        record = _load_fixture("delivery_drop_wrong_model.jsonl")
        unwrapped = unwrap_delivery_record(record)
        assert unwrapped is not None
        trajectory = extract(unwrapped)
        passed, reasons = evaluate(trajectory)
        self.assertFalse(passed)
        self.assertGreaterEqual(len(reasons), 1)
        self.assertEqual(len(RULES), 7)

    def test_dedup_pair_shares_session_id(self) -> None:
        lines = (FIXTURES_DATA / "delivery_dedup_pair.jsonl").read_text(encoding="utf-8").splitlines()
        trajectories = []
        for line in lines:
            record = json.loads(line)
            unwrapped = unwrap_delivery_record(record)
            assert unwrapped is not None
            trajectories.append(extract(unwrapped))

        sid_a = compute_session_id(trajectories[0])
        sid_b = compute_session_id(trajectories[1])
        self.assertIsNotNone(sid_a)
        self.assertEqual(sid_a, sid_b)
        self.assertGreater(
            len(trajectories[1].get("messages") or []),
            len(trajectories[0].get("messages") or []),
        )


if __name__ == "__main__":
    unittest.main()
