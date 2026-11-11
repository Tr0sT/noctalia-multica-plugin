import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
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



class RestrictedTaskTests(unittest.TestCase):
    def test_private_project_run_keeps_presence_without_reading_hidden_metadata(self):
        # Newer Multica emits a restricted projection for inaccessible projects.
        # It intentionally retains agent/runtime/status, but denies transcript reads.
        secret = "PRIVATE PROJECT TITLE"
        restricted = {
            "id": "task-private", "agent_id": "agent", "runtime_id": "local",
            "status": "running", "restricted": True, "priority": 5,
            "issue_id": "private-issue", "chat_session_id": "private-chat",
            "trigger_summary": secret, "thread_name": secret, "kind": "hidden",
            "created_at": "2026-11-11T01:00:00Z",
        }
        requests = []

        def get(path, workspace=None):
            requests.append((path, workspace))
            if path == "/api/agents":
                return [{"id": "agent", "name": "Worker", "runtime_id": "local"}]
            if path == "/api/agent-task-snapshot":
                return [restricted]
            self.fail("Restricted task must not request its issue/chat metadata")

        client = SimpleNamespace(app="https://example.invalid", name="", get=get)
        workspace = {"id": "ws", "slug": "space", "name": "Workspace"}
        tasks, agents = bridge.workspace_snapshot(
            client, workspace, {"runtimes": ["local"]})
        self.assertEqual(1, len(tasks))
        self.assertEqual("running", tasks[0]["status"])
        self.assertEqual("Worker", tasks[0]["agent"])
        self.assertTrue(tasks[0]["restricted"])
        self.assertEqual("Task", tasks[0]["title"])
        self.assertEqual("", tasks[0]["identifier"])
        self.assertEqual("", tasks[0]["url"])
        self.assertEqual("", tasks[0]["detail_error"])
        self.assertNotIn(secret, json.dumps(tasks))
        self.assertEqual([
            ("/api/agents", "ws"), ("/api/agent-task-snapshot", "ws")], requests)
        self.assertEqual("Worker", agents[0]["name"])


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
            self.assertFalse(result["can_control"])


class DaemonControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / ".multica"
        for directory in [self.root, self.root / "profiles/nord"]:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "config.json").write_text(json.dumps({
                "server_url": "https://example.invalid", "token": "SECRET-TEST-CREDENTIAL"}))
        self.health = {"status": "running", "profile": "nord", "pid": 23, "daemon_id": "local",
                       "active_task_count": 0, "running_task_count": 0, "resource_wait_task_count": 0,
                       "device_name": "pc", "server_url": "https://example.invalid", "workspaces": []}
        for target, attribute, mock_name, kwargs in [
            (bridge.Path, "home", "home_mock", {"return_value": self.home}),
            (bridge.shutil, "which", "which", {"return_value": "/path with spaces/multica"}),
            (bridge, "local_health", "local_health", {"return_value": self.health}),
            (bridge.subprocess, "run", "run_cli", {"return_value": SimpleNamespace(returncode=0)}),
        ]:
            mocked = patch.object(target, attribute, **kwargs)
            instance = mocked.start()
            self.addCleanup(mocked.stop)
            setattr(self, mock_name, instance)

    def test_stop_rechecks_active_tasks_before_requesting_confirmation(self):
        # The last UI snapshot may have been idle. Waiting tasks count too.
        self.health.update(active_task_count=2, running_task_count=1, resource_wait_task_count=1)
        result = bridge.daemon_control(self.root, "nord", "stop")
        self.assertEqual({"confirmation_required": True, "active": 2}, result)
        self.run_cli.assert_not_called()

    def test_confirmed_stop_targets_exact_profile_without_shell_or_force(self):
        self.health["active_task_count"] = 2
        for profile in ["", "nord"]:
            with self.subTest(profile=profile):
                result = bridge.daemon_control(self.root, profile, "stop", allow_active=True)
                self.assertTrue(result["ok"])
                self.assertEqual(profile, result["profile"])
                self.assertEqual(["/path with spaces/multica", "--profile", profile,
                                  "--server-url", "https://example.invalid", "daemon", "stop"],
                                 self.run_cli.call_args.args[0])
                self.assertNotIn("--force", self.run_cli.call_args.args[0])
                self.assertFalse(self.run_cli.call_args.kwargs.get("shell", False))
                for stream in ["stdin", "stdout", "stderr"]:
                    self.assertEqual(bridge.subprocess.DEVNULL, self.run_cli.call_args.kwargs[stream])
                self.assertNotIn("SECRET", repr(self.run_cli.call_args))

    def test_start_uses_background_cli_when_offline_and_is_noop_when_online(self):
        self.local_health.side_effect = bridge.MonitorError("unavailable")
        with patch.dict("os.environ", {"MULTICA_SERVER_URL": "https://different.invalid"}):
            self.assertTrue(bridge.daemon_control(self.root, "nord", "start")["ok"])
        self.assertEqual(["/path with spaces/multica", "--profile", "nord",
                          "--server-url", "https://example.invalid", "daemon", "start"],
                         self.run_cli.call_args.args[0])
        self.run_cli.reset_mock()
        self.local_health.side_effect = None
        self.health["status"] = "starting"
        self.assertTrue(bridge.daemon_control(self.root, "nord", "start")["ok"])
        self.run_cli.assert_not_called()

    def test_unverified_daemon_and_desktop_owned_daemon_cannot_be_stopped(self):
        self.local_health.side_effect = bridge.MonitorError("profile identity does not match")
        with self.assertRaisesRegex(bridge.MonitorError, "daemon.unavailable"):
            bridge.daemon_control(self.root, "nord", "stop", allow_active=True)
        self.local_health.side_effect = None
        self.health["server_url"] = "https://different.invalid"
        with self.assertRaisesRegex(bridge.MonitorError, "daemon.server-mismatch"):
            bridge.daemon_control(self.root, "nord", "stop", allow_active=True)
        self.health.update(server_url="https://example.invalid", launched_by="desktop")
        for action in ["start", "stop"]:
            with self.subTest(action=action), self.assertRaisesRegex(bridge.MonitorError, "daemon.managed"):
                bridge.daemon_control(self.root, "nord", action, allow_active=True)
        self.run_cli.assert_not_called()

    def test_invalid_inputs_and_missing_cli_never_launch_a_process(self):
        for root, name, action in [(self.home / "elsewhere", "nord", "start"),
                                   (self.root, "../nord", "stop"),
                                   (self.root, "nord", "restart"),
                                   (self.root, "missing", "start")]:
            with self.subTest(root=root, name=name, action=action), self.assertRaises(bridge.MonitorError):
                bridge.daemon_control(root, name, action)
        self.which.return_value = None
        with self.assertRaisesRegex(bridge.MonitorError, "daemon.cli-missing"):
            bridge.daemon_control(self.root, "nord", "stop")
        self.run_cli.assert_not_called()

    def test_command_failures_and_timeouts_do_not_expose_cli_output(self):
        self.run_cli.return_value = SimpleNamespace(returncode=1, stderr="SECRET-TEST-CREDENTIAL")
        with self.assertRaises(bridge.MonitorError) as failure:
            bridge.daemon_control(self.root, "nord", "stop")
        self.assertEqual("daemon.command-failed", str(failure.exception))
        self.run_cli.side_effect = bridge.subprocess.TimeoutExpired("multica", 50, stderr="SECRET-TEST-CREDENTIAL")
        with self.assertRaises(bridge.MonitorError) as timeout:
            bridge.daemon_control(self.root, "nord", "stop")
        self.assertEqual("daemon.timeout", str(timeout.exception))

    def test_slow_detached_start_must_be_verified_before_acceptance(self):
        self.run_cli.side_effect = bridge.subprocess.TimeoutExpired("multica", 50)
        for final in [dict(self.health, status="starting"),
                      dict(self.health, server_url="https://different.invalid"),
                      bridge.MonitorError("unavailable")]:
            self.local_health.side_effect = [bridge.MonitorError("unavailable"), final]
            with self.subTest(final=final):
                if isinstance(final, dict) and final.get("status") == "starting":
                    self.assertTrue(bridge.daemon_control(self.root, "nord", "start")["ok"])
                else:
                    with self.assertRaisesRegex(bridge.MonitorError, "daemon.timeout"):
                        bridge.daemon_control(self.root, "nord", "start")
                self.assertEqual(50, self.run_cli.call_args.kwargs["timeout"])

    def test_offline_profile_can_start_and_server_failure_does_not_disable_local_stop(self):
        self.local_health.side_effect = bridge.MonitorError("unavailable")
        offline = bridge.snapshot_profile(self.root, "nord")
        self.assertFalse(offline["online"])
        self.assertTrue(offline["can_control"])
        self.local_health.side_effect = None
        with patch.object(bridge.Client, "get", side_effect=bridge.MonitorError("HTTP 503")):
            online = bridge.snapshot_profile(self.root, "nord")
        self.assertTrue(online["online"])
        self.assertTrue(online["can_control"])
        self.assertIn("HTTP 503", online["errors"])

    def test_snapshot_explains_disabled_controls(self):
        with patch.object(bridge.Client, "get", return_value=[]):
            self.which.return_value = None
            missing = bridge.snapshot_profile(self.root, "nord")
            self.assertFalse(missing["can_control"])
            self.assertEqual("daemon.cli-missing", missing["control_error"])
            self.which.return_value = "/path/multica"
            self.health["launched_by"] = "desktop"
            managed = bridge.snapshot_profile(self.root, "nord")
            self.assertFalse(managed["can_control"])
            self.assertEqual("daemon.managed", managed["control_error"])


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
