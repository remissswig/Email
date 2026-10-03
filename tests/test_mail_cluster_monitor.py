import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT_DIR / "scripts" / "mail_cluster_monitor.py"
SPEC = importlib.util.spec_from_file_location("mail_cluster_monitor_test_module", MONITOR_PATH)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class MailClusterMonitorTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "node-a:5000": 1,
            "node-b:5000": 1,
        }
        self.ok = monitor.CheckResult(
            "node:node-a:5000/health/business",
            True,
            0.1,
            200,
            "ok",
        )
        self.ok_b = monitor.CheckResult(
            "node:node-b:5000/health/business",
            True,
            0.1,
            200,
            "ok",
        )
        self.bad = monitor.CheckResult(
            "node:node-a:5000/health/business",
            False,
            0.1,
            503,
            "bad",
        )

    def test_requires_two_failures_before_offline(self):
        state = monitor.load_monitor_state(self.expected, self.expected)
        with patch.object(monitor.time, "time", return_value=100.0):
            first = monitor.update_monitor_state(
                state,
                [self.bad, self.ok_b],
                weights=self.expected,
                failure_threshold=2,
                success_threshold=2,
                cooldown_seconds=0,
            )
        self.assertEqual(first, self.expected)
        self.assertEqual(state["nodes"]["node-a:5000"]["failure_count"], 1)

        with patch.object(monitor.time, "time", return_value=101.0):
            second = monitor.update_monitor_state(
                state,
                [self.bad, self.ok_b],
                weights=self.expected,
                failure_threshold=2,
                success_threshold=2,
                cooldown_seconds=0,
            )
        self.assertEqual(second, {"node-b:5000": 1})
        self.assertFalse(state["nodes"]["node-a:5000"]["online"])

    def test_requires_two_successes_before_recovery(self):
        state = monitor.load_monitor_state(self.expected, {})
        state["nodes"]["node-a:5000"]["online"] = False
        state["nodes"]["node-a:5000"]["cooldown_until"] = 0

        with patch.object(monitor.time, "time", return_value=100.0):
            first = monitor.update_monitor_state(
                state,
                [self.ok],
                weights=self.expected,
                failure_threshold=2,
                success_threshold=2,
                cooldown_seconds=0,
            )
        self.assertNotIn("node-a:5000", first)

        with patch.object(monitor.time, "time", return_value=101.0):
            second = monitor.update_monitor_state(
                state,
                [self.ok],
                weights=self.expected,
                failure_threshold=2,
                success_threshold=2,
                cooldown_seconds=0,
            )
        self.assertIn("node-a:5000", second)

    def test_state_is_written_atomically_and_reloaded(self):
        state = monitor.load_monitor_state(self.expected, self.expected)
        state["nodes"]["node-a:5000"]["failure_count"] = 1
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "state.json"
            with patch.dict(
                monitor.os.environ,
                {"MAIL_CLUSTER_STATE_FILE": str(state_path)},
                clear=False,
            ):
                monitor.save_monitor_state(state)
                reloaded = monitor.load_monitor_state(self.expected, {})
        self.assertEqual(reloaded["nodes"]["node-a:5000"]["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
