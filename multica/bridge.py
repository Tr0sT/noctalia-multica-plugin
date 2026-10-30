#!/usr/bin/env python3
"""Read-only Multica bridge. Credentials never leave this process."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


ACTIVE = {"running", "dispatched", "waiting_local_directory"}
QUEUED = {"queued", "deferred"}
RECENT = {"completed", "failed"}
MAX_RESPONSE = 32 * 1024 * 1024
MAX_LOG_BYTES = 100_000
MODEL_MESSAGES = {"text", "reasoning", "thinking"}


class MonitorError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must not forward a profile's bearer token to another host.
        return None


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MonitorError(f"Cannot read {path.name}") from exc


def request_json(url, token=None, workspace=None, local=False):
    headers = {"Accept": "application/json", "User-Agent": "noctalia-multica-plugin/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if workspace:
        headers["X-Workspace-ID"] = workspace
    opener = build_opener(NoRedirect(), *([ProxyHandler({})] if local else []))
    try:
        with opener.open(Request(url, headers=headers), timeout=2 if local else 8) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise MonitorError("Response exceeds 32 MiB; open this run in Multica")
        return json.loads(raw)
    except HTTPError as exc:
        # Do not echo response bodies: auth gateways may put secrets in them.
        raise MonitorError(f"HTTP {exc.code} ({urlsplit(url).hostname})") from exc
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise MonitorError("Daemon unavailable" if local else "Server unavailable or timed out") from exc
    except ValueError as exc:
        raise MonitorError("Invalid JSON response") from exc


def object_value(value):
    if not isinstance(value, dict):
        raise MonitorError("Expected a JSON object")
    return value


def object_list(value):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise MonitorError("Expected a JSON array of objects")
    return value


def required_string(value, key):
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise MonitorError(f"Missing or invalid {key}")
    return result


def base_url(value):
    if not isinstance(value, str):
        raise MonitorError("Missing server URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MonitorError("Invalid server URL")
    return value.rstrip("/")


def profile_directory(root, name):
    if not name:
        return root
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise MonitorError("Invalid profile name")
    return root / "profiles" / name


def profile_names(root):
    names = [""] if (root / "config.json").exists() else []
    names += sorted(p.name for p in (root / "profiles").glob("*") if (p / "config.json").is_file())
    return names


def local_health(directory, name):
    fallback = 19514 if not name else 19515 + sum(name.encode()) % 1000
    probes = []
    try:
        record = object_value(read_json(directory / "daemon.port"))
        port, pid = record.get("port"), record.get("pid")
        if type(port) is int and 0 < port < 65536 and type(pid) is int and pid > 0:
            probes.append((port, pid))
    except MonitorError:
        pass
    probes.append((fallback, None))
    for port, expected_pid in probes:
        try:
            health = object_value(request_json(f"http://127.0.0.1:{port}/health", local=True))
            if health.get("profile") != name or (expected_pid and health.get("pid") != expected_pid):
                continue
            if health.get("status") not in {"running", "starting"}:
                continue
            required_string(health, "daemon_id")
            for key in ("active_task_count", "running_task_count", "resource_wait_task_count"):
                if type(health.get(key)) is not int or health[key] < 0:
                    raise MonitorError(f"Missing or invalid {key}")
            for ws in object_list(health.get("workspaces") or []):
                required_string(ws, "id")
                if not isinstance(ws.get("runtimes"), list) or any(not isinstance(x, str) for x in ws["runtimes"]):
                    raise MonitorError("Invalid runtime list")
            return health
        except MonitorError:
            continue
    raise MonitorError("Daemon unavailable or profile identity does not match")


class Client:
    def __init__(self, root, name):
        directory = profile_directory(root, name)
        config = object_value(read_json(directory / "config.json"))
        self.server = base_url(config.get("server_url"))
        self.app = base_url(config.get("app_url") or self.server)
        self.token = required_string(config, "token")
        self.name = name
        self.directory = directory

    def get(self, path, workspace=None):
        return request_json(self.server + path, self.token, workspace)


def belongs_here(task, agents, runtimes):
    status = required_string(task, "status")
    required_string(task, "id")
    agent = agents.get(required_string(task, "agent_id"), {})
    # Queues follow the agent's current binding; claimed runs keep their own runtime.
    if status in QUEUED:
        return agent.get("runtime_id") in runtimes
    return status in ACTIVE | RECENT and task.get("runtime_id") in runtimes


def task_summary(client, workspace, task, agent):
    task_id = required_string(task, "id")
    ws_id = required_string(workspace, "id")
    slug = workspace.get("slug") or ws_id
    link = client.app + "/" + quote(slug, safe="")
    title = task.get("trigger_summary") or task.get("thread_name") or task.get("kind") or "Task"
    identifier, detail_error = "", ""
    if task.get("issue_id"):
        issue_id = quote(task["issue_id"], safe="")
        link += "/issues/" + issue_id
        try:
            issue = object_value(client.get("/api/issues/" + issue_id, ws_id))
            title = required_string(issue, "title")
            identifier = issue.get("identifier") or ""
        except MonitorError as exc:
            title = task.get("issue_identifier") or task["issue_id"]
            detail_error = str(exc)
    elif task.get("chat_session_id"):
        session_id = quote(task["chat_session_id"], safe="")
        link += "/chat?session=" + session_id
        try:
            session = object_value(client.get("/api/chat/sessions/" + session_id, ws_id))
            title = session.get("title") or title
        except MonitorError as exc:
            detail_error = str(exc)
    else:
        link += "/agents/" + quote(task["agent_id"], safe="")
    return {
        "id": task_id, "key": client.name + ":" + task_id, "profile": client.name,
        "workspace_id": ws_id, "workspace": workspace.get("name") or slug,
        "agent": agent.get("name") or task["agent_id"], "agent_id": task["agent_id"],
        "title": title, "identifier": identifier, "status": task["status"],
        "priority": task.get("priority") or 0, "created_at": task.get("created_at") or "",
        "started_at": task.get("started_at") or "", "completed_at": task.get("completed_at") or "",
        "url": link, "detail_error": detail_error,
    }


def workspace_snapshot(client, ws, health_ws):
    ws_id = required_string(ws, "id")
    runtimes = set(health_ws["runtimes"])
    agents = {required_string(a, "id"): a for a in object_list(client.get("/api/agents", ws_id))}
    tasks = object_list(client.get("/api/agent-task-snapshot", ws_id))
    tasks = [t for t in tasks if belongs_here(t, agents, runtimes)]
    active = [t for t in tasks if t["status"] in ACTIVE | QUEUED]
    recent = sorted((t for t in tasks if t["status"] in RECENT), key=lambda t: t.get("completed_at") or "", reverse=True)[:5]
    # Metadata reads are limited to local runs; unrelated agents' prompts never leave the helper.
    summaries = [task_summary(client, ws, t, agents.get(t["agent_id"], {})) for t in active + recent]
    bound = [{"name": a.get("name") or a["id"], "workspace": ws.get("name") or ws_id}
             for a in agents.values() if a.get("runtime_id") in runtimes and not a.get("archived_at")]
    return summaries, bound


def snapshot_profile(root, name):
    result = {"name": name, "online": False, "errors": [], "agents": [], "tasks": []}
    try:
        client = Client(root, name)
        health = local_health(client.directory, name)
        result.update(online=True, status=health["status"], device=health.get("device_name") or "",
                      running=health["running_task_count"], waiting=health["resource_wait_task_count"],
                      active=health["active_task_count"])
        if base_url(health.get("server_url")) != client.server:
            raise MonitorError("Daemon server differs from profile config; refresh the Multica profile")
        workspaces = {required_string(w, "id"): w for w in object_list(client.get("/api/workspaces"))}
        health_workspaces = health.get("workspaces") or []

        def collect(health_ws):
            ws_id = health_ws["id"]
            ws = workspaces.get(ws_id)
            if ws is None:
                return [], [], f"{ws_id}: workspace access unavailable"
            try:
                tasks, agents = workspace_snapshot(client, ws, health_ws)
                return tasks, agents, None
            except MonitorError as exc:
                return [], [], f"{ws.get('name') or ws_id}: {exc}"

        with ThreadPoolExecutor(max_workers=6) as pool:
            for tasks, agents, error in pool.map(collect, health_workspaces):
                result["tasks"].extend(tasks)
                result["agents"].extend(agents)
                if error:
                    result["errors"].append(error)
    except MonitorError as exc:
        result["errors"].append(str(exc))
    return result


def snapshot(root):
    names = profile_names(root)
    with ThreadPoolExecutor(max_workers=4) as pool:
        profiles = list(pool.map(lambda name: snapshot_profile(root, name), names))
    tasks = [task for p in profiles for task in p.pop("tasks")]
    active = [t for t in tasks if t["status"] in ACTIVE]
    queue = sorted((t for t in tasks if t["status"] in QUEUED), key=lambda t: (-t["priority"], t["created_at"], t["id"]))
    recent = sorted((t for t in tasks if t["status"] in RECENT), key=lambda t: t["completed_at"], reverse=True)[:10]
    return {"profiles": profiles, "active": active, "queue": queue, "recent": recent,
            "running": sum(p.get("running", 0) for p in profiles),
            "waiting": sum(p.get("waiting", 0) for p in profiles),
            "updated_at": int(time.time())}


def log_page(messages, since=None, before=None):
    rows = object_list(messages)
    if any(type(m.get("seq")) is not int or m["seq"] < 0 for m in rows):
        raise MonitorError("Invalid message sequence")
    rows = sorted(rows, key=lambda m: m["seq"])
    if since is not None:
        rows = [m for m in rows if m["seq"] > since]
    if before is not None:
        rows = [m for m in rows if m["seq"] < before]
    last_seq = rows[-1]["seq"] if rows else since
    rows = [m for m in rows if m.get("type") in MODEL_MESSAGES and m.get("content")]
    candidates = rows if since is not None else list(reversed(rows))
    page, used = [], 0
    for message in candidates:
        text = message["content"]
        if not isinstance(text, str):
            raise MonitorError("Invalid message content")
        # shortcut: exceptionally long messages are previewed; the task link opens the full transcript.
        clipped = len(text) > 16000
        row = {"seq": message["seq"], "type": message.get("type") or "text",
               "text": text[:16000],
               "created_at": message.get("created_at") or "",
               "truncated": clipped}
        size = len(json.dumps(row, ensure_ascii=False).encode())
        if page and (used + size > MAX_LOG_BYTES or len(page) >= 60):
            break
        page.append(row)
        used += size
    page.sort(key=lambda m: m["seq"])
    has_more = len(page) < len(rows)
    return {"messages": page, "has_more": has_more,
            "first_seq": page[0]["seq"] if page else None,
            "last_seq": page[-1]["seq"] if since is not None and has_more else last_seq}


def logs(root, name, workspace, task, since=None, before=None):
    try:
        uuid.UUID(workspace)
        uuid.UUID(task)
    except (ValueError, TypeError) as exc:
        raise MonitorError("Invalid task or workspace ID") from exc
    client = Client(root, name)
    path = "/api/tasks/" + task + "/messages"
    if since is not None:
        path += "?since=" + str(since)
    return log_page(client.get(path, workspace), since, before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "logs"])
    parser.add_argument("--root", type=Path, default=Path.home() / ".multica")
    parser.add_argument("--profile", default="")
    parser.add_argument("--workspace")
    parser.add_argument("--task")
    parser.add_argument("--since", type=int)
    parser.add_argument("--before", type=int)
    args = parser.parse_args()
    try:
        data = snapshot(args.root) if args.command == "snapshot" else logs(
            args.root, args.profile, args.workspace, args.task, args.since, args.before)
    except MonitorError as exc:
        data = {"error": str(exc)}
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
