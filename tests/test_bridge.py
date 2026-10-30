import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("bridge", Path(__file__).parents[1] / "multica/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class RuntimeScopeTests(unittest.TestCase):
    def test_queue_follows_current_binding_but_running_task_keeps_claimed_runtime(self):
        agents = {"local": {"runtime_id": "here"}, "remote": {"runtime_id": "there"}}
        cases = [
            ("queued", "local", "there", True),
            ("queued", "remote", "here", False),
            ("deferred", "local", "", True),
            ("running", "remote", "here", True),
            ("running", "local", "there", False),
            ("dispatched", "local", "here", True),
            ("waiting_local_directory", "local", "here", True),
            ("completed", "remote", "here", True),
            ("failed", "local", "there", False),
            ("cancelled", "local", "here", False),
            ("unknown", "local", "here", False),
        ]
        for status, agent, runtime, expected in cases:
            with self.subTest(status=status, agent=agent, runtime=runtime):
                self.assertEqual(expected, bridge.belongs_here(
                    {"id": "task", "status": status, "agent_id": agent, "runtime_id": runtime}, agents, {"here"}))

    def test_malformed_snapshot_is_an_error_not_empty_queue(self):
        for value in [{}, None, ["broken"]]:
            with self.subTest(value=value), self.assertRaises(bridge.MonitorError):
                bridge.object_list(value)
        with self.assertRaises(bridge.MonitorError):
            bridge.belongs_here({"id": "x", "agent_id": "agent"}, {}, set())

    def test_profile_traversal_is_rejected(self):
        for name in ["..", "../nord", "/tmp", "a\\b"]:
            with self.subTest(name=name), self.assertRaises(bridge.MonitorError):
                bridge.profile_directory(Path("/tmp/profiles"), name)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "daemon.port").write_text(json.dumps({"port": 22000, "pid": 23}))

    def health(self, **overrides):
        return dict({"status": "running", "profile": "nord", "pid": 23, "daemon_id": "local",
                     "active_task_count": 1, "running_task_count": 1, "resource_wait_task_count": 0,
                     "workspaces": []}, **overrides)

    def test_port_record_is_validated_and_stale_record_falls_back(self):
        with patch.object(bridge, "request_json", side_effect=[self.health(profile="other"), self.health(pid=99)]) as get:
            self.assertEqual(99, bridge.local_health(self.root, "nord")["pid"])
            self.assertIn(":22000/health", get.call_args_list[0].args[0])
            self.assertNotIn(":22000/health", get.call_args_list[1].args[0])

    def test_profile_collision_never_leaks_foreign_tasks(self):
        with patch.object(bridge, "request_json", return_value=self.health(profile="other")):
            with self.assertRaises(bridge.MonitorError):
                bridge.local_health(self.root, "nord")

    def test_bad_counters_are_not_reported_as_idle(self):
        with patch.object(bridge, "request_json", return_value=self.health(running_task_count="0")):
            with self.assertRaises(bridge.MonitorError):
                bridge.local_health(self.root, "nord")


class SnapshotTests(unittest.TestCase):
    def test_one_failing_profile_does_not_hide_other_profile_and_no_credentials_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in [root, root / "profiles/nord"]:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "config.json").write_text(json.dumps({
                    "server_url": "https://example.invalid", "token": "SECRET-TEST-CREDENTIAL"}))
            health = {"status": "running", "device_name": "local", "running_task_count": 1,
                      "resource_wait_task_count": 0, "active_task_count": 1,
                      "server_url": "https://example.invalid", "workspaces": [{"id": "ws", "runtimes": ["local"]}]}

            def get(client, path, workspace=None):
                if client.name == "nord":
                    raise bridge.MonitorError("HTTP 401")
                if path == "/api/workspaces":
                    return [{"id": "ws", "name": "Workspace", "slug": "space"}]
                if path == "/api/agents":
                    return [{"id": "a", "name": "Agent", "runtime_id": "local", "instructions": "PRIVATE PROMPT"}]
                if path == "/api/agent-task-snapshot":
                    return [{"id": "task", "agent_id": "a", "runtime_id": "local", "status": "running"}]
                self.fail(path)

            with patch.object(bridge, "local_health", return_value=health), patch.object(bridge.Client, "get", new=get):
                result = bridge.snapshot(root)
            self.assertEqual(1, len(result["active"]))
            self.assertEqual(["HTTP 401"], result["profiles"][1]["errors"])
            serialized = json.dumps(result)
            self.assertNotIn("SECRET-TEST-CREDENTIAL", serialized)
            self.assertNotIn("PRIVATE PROMPT", serialized)

    def test_server_mismatch_never_receives_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"server_url": "https://a.invalid", "token": "SECRET"}))
            health = {"status": "running", "running_task_count": 0, "resource_wait_task_count": 0,
                      "active_task_count": 0, "server_url": "https://b.invalid"}
            with patch.object(bridge, "local_health", return_value=health), patch.object(bridge.Client, "get") as get:
                result = bridge.snapshot_profile(root, "")
            get.assert_not_called()
            self.assertIn("differs", result["errors"][0])


class LogTests(unittest.TestCase):
    def test_history_pages_and_incremental_catchup_have_no_gaps(self):
        messages = [{"seq": n, "type": "text", "content": f"Message {n}"} for n in range(1, 151)]
        first = bridge.log_page(messages)
        self.assertEqual(list(range(91, 151)), [m["seq"] for m in first["messages"]])
        older = bridge.log_page(messages, before=first["first_seq"])
        self.assertEqual(list(range(31, 91)), [m["seq"] for m in older["messages"]])
        newest = bridge.log_page(messages, since=5)
        self.assertEqual(list(range(6, 66)), [m["seq"] for m in newest["messages"]])
        self.assertTrue(newest["has_more"])

    def test_only_model_text_is_exposed_and_tool_only_tail_advances_cursor(self):
        page = bridge.log_page([
            {"seq": 1, "type": "reasoning", "content": "Plan"},
            {"seq": 2, "type": "tool_use", "tool": "exec", "input": {"cmd": "pwd"}},
            {"seq": 3, "type": "tool_result", "output": "a" * 20000},
            {"seq": 4, "type": "text", "content": "a" * 20000},
            {"seq": 5, "type": "thinking", "content": "Next step"},
            {"seq": 6, "type": "tool_use", "content": "SECRET COMMAND"},
        ])
        result = page["messages"]
        self.assertEqual([1, 4, 5], [m["seq"] for m in result])
        self.assertEqual("Plan", result[0]["text"])
        self.assertTrue(result[1]["truncated"])
        self.assertEqual(6, page["last_seq"])
        self.assertNotIn("SECRET COMMAND", json.dumps(page))
        tail = bridge.log_page([{"seq": 7, "type": "tool_result", "output": "hidden"}], since=6)
        self.assertEqual([], tail["messages"])
        self.assertEqual(7, tail["last_seq"])

    def test_bad_message_or_task_ids_are_errors(self):
        with self.assertRaises(bridge.MonitorError):
            bridge.log_page([{"seq": "2"}])
        with self.assertRaises(bridge.MonitorError):
            bridge.logs(Path("/tmp"), "", "invalid", "invalid")


if __name__ == "__main__":
    unittest.main()
