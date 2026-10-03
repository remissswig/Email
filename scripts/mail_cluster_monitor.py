#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request


DEFAULT_NODES = {
    "172.17.0.1:5000": 2,
    "68.221.72.212:5000": 1,
    "91.233.10.96:5005": 1,
    "172.93.219.182:5000": 1,
    "172.188.64.234:5000": 1,
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    seconds: float
    status: int | None
    message: str


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5.0) -> tuple[int, Any, str]:
    req = request.Request(url, headers=headers or {})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1024 * 1024)
            text = body.decode("utf-8", "replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            return int(resp.status), payload, text
    except error.HTTPError as exc:
        text = exc.read(8192).decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        return int(exc.code), payload, text


def post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 5.0) -> int:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        return int(resp.status)


def put_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 5.0) -> int:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        return int(resp.status)


def load_expected_nodes() -> dict[str, int]:
    raw = os.getenv("MAIL_CLUSTER_EXPECTED_NODES", "").strip()
    if not raw:
        return dict(DEFAULT_NODES)
    try:
        parsed = json.loads(raw)
        return {str(key): int(value) for key, value in dict(parsed).items()}
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(DEFAULT_NODES)


def apisix_management_enabled() -> bool:
    return os.getenv("MAIL_CLUSTER_APISIX_MANAGE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def apisix_admin_context() -> tuple[str, str, dict[str, str]]:
    admin_url = os.getenv("APISIX_ADMIN_URL", "http://127.0.0.1:9180/apisix/admin").rstrip("/")
    upstream_id = os.getenv("APISIX_UPSTREAM_ID", "00000000000000000015").strip()
    api_key = os.getenv("APISIX_API_KEY", "").strip()
    return admin_url, upstream_id, {"X-API-KEY": api_key}


def read_current_upstream_nodes() -> tuple[dict[str, int] | None, list[str]]:
    if not apisix_management_enabled():
        return None, []
    admin_url, upstream_id, headers = apisix_admin_context()
    if not upstream_id or not headers["X-API-KEY"]:
        return None, ["APISIX upstream state unavailable: missing APISIX_UPSTREAM_ID or APISIX_API_KEY"]
    try:
        status, payload, _text = http_json(
            f"{admin_url}/upstreams/{upstream_id}",
            headers=headers,
            timeout=5,
        )
    except Exception as exc:
        return None, [f"APISIX upstream read failed: {exc!r}"]
    if status != 200 or not isinstance(payload, dict):
        return None, [f"APISIX upstream read failed status={status}"]
    value = payload.get("value") or {}
    return dict(value.get("nodes") or {}), []


def monitor_state_path() -> str:
    return (
        os.getenv(
            "MAIL_CLUSTER_STATE_FILE",
            "/var/lib/mail-cluster-monitor/state.json",
        ).strip()
        or "/var/lib/mail-cluster-monitor/state.json"
    )


def load_monitor_state(
    expected_nodes: dict[str, int],
    current_nodes: dict[str, int] | None,
) -> dict[str, Any]:
    path = monitor_state_path()
    payload: dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            candidate = json.load(handle)
        if isinstance(candidate, dict):
            payload = candidate
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}

    stored_nodes = payload.get("nodes")
    if not isinstance(stored_nodes, dict):
        stored_nodes = {}

    def safe_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    nodes: dict[str, dict[str, Any]] = {}
    for node in expected_nodes:
        stored = stored_nodes.get(node)
        if not isinstance(stored, dict):
            stored = {}
        initial_online = node in current_nodes if current_nodes is not None else True
        nodes[node] = {
            "online": bool(stored.get("online", initial_online)),
            "failure_count": safe_int(stored.get("failure_count", 0)),
            "success_count": safe_int(stored.get("success_count", 0)),
            "cooldown_until": safe_float(stored.get("cooldown_until", 0.0)),
            "last_change_at": safe_float(stored.get("last_change_at", 0.0)),
        }
    return {"version": 1, "nodes": nodes}


def save_monitor_state(state: dict[str, Any]) -> None:
    path = monitor_state_path()
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"monitor state save failed: {exc!r}", file=sys.stderr, flush=True)


def update_monitor_state(
    state: dict[str, Any],
    node_results: list[CheckResult],
    *,
    weights: dict[str, int],
    failure_threshold: int,
    success_threshold: int,
    cooldown_seconds: float,
) -> dict[str, int]:
    now = time.time()
    active_nodes: dict[str, int] = {}
    for result in node_results:
        node = result.name.split(":", 1)[1].split("/", 1)[0]
        entry = state["nodes"][node]
        if result.ok:
            entry["failure_count"] = 0
            entry["success_count"] = int(entry.get("success_count", 0)) + 1
            if (
                not entry["online"]
                and entry["success_count"] >= success_threshold
                and now >= float(entry.get("cooldown_until", 0.0) or 0.0)
            ):
                entry["online"] = True
                entry["last_change_at"] = now
                entry["cooldown_until"] = now + cooldown_seconds
        else:
            entry["success_count"] = 0
            entry["failure_count"] = int(entry.get("failure_count", 0)) + 1
            if (
                entry["online"]
                and entry["failure_count"] >= failure_threshold
                and now >= float(entry.get("cooldown_until", 0.0) or 0.0)
            ):
                entry["online"] = False
                entry["last_change_at"] = now
                entry["cooldown_until"] = now + cooldown_seconds
        if entry["online"]:
            active_nodes[node] = weights[node]
    return active_nodes


def upstream_payload(expected_nodes: dict[str, int]) -> dict[str, Any]:
    healthcheck_path = os.getenv("MAIL_CLUSTER_APISIX_HEALTHCHECK_PATH", "/health/business").strip()
    if not healthcheck_path.startswith("/"):
        healthcheck_path = "/health/business"
    return {
        "name": os.getenv("MAIL_CLUSTER_UPSTREAM_NAME", "mail-cluster"),
        "desc": os.getenv(
            "MAIL_CLUSTER_UPSTREAM_DESC",
            "mail-cluster active business-ready nodes; managed healthcheck enabled",
        ),
        "type": "chash",
        "hash_on": "vars",
        "key": "request_uri",
        "scheme": "http",
        "pass_host": "pass",
        "nodes": expected_nodes,
        "checks": {
            "active": {
                "type": "http",
                "http_path": healthcheck_path,
                "timeout": 2,
                "concurrency": 4,
                "healthy": {
                    "interval": 5,
                    "successes": 2,
                    "http_statuses": [200, 302],
                },
                "unhealthy": {
                    "interval": 3,
                    "http_failures": 2,
                    "tcp_failures": 2,
                    "timeouts": 2,
                    "http_statuses": [429, 500, 502, 503, 504],
                },
            },
            "passive": {
                "type": "http",
                "healthy": {
                    "successes": 2,
                    "http_statuses": [200, 302],
                },
                "unhealthy": {
                    "http_failures": 3,
                    "tcp_failures": 2,
                    "timeouts": 2,
                    "http_statuses": [500, 502, 503, 504],
                },
            },
        },
    }


def check_node(node: str, timeout: float, path: str) -> CheckResult:
    if not path.startswith("/"):
        path = "/health/business"
    url = f"http://{node}{path}"
    started = time.time()
    try:
        status, payload, text = http_json(url, timeout=timeout)
        seconds = time.time() - started
        ok = status == 200 and isinstance(payload, dict) and payload.get("success") is True
        message = text[:200].replace("\n", " ")
        return CheckResult(f"node:{node}{path}", ok, seconds, status, message)
    except Exception as exc:
        return CheckResult(f"node:{node}{path}", False, time.time() - started, None, repr(exc))


def check_ready(node: str, timeout: float) -> CheckResult:
    return check_node(node, timeout, "/health/ready")


def check_probe(url: str, timeout: float, slow_seconds: float) -> CheckResult:
    started = time.time()
    try:
        status, payload, text = http_json(url, timeout=timeout)
        seconds = time.time() - started
        ok = 200 <= status < 300 and seconds <= slow_seconds
        node = ""
        if isinstance(payload, dict):
            node = str(payload.get("node") or payload.get("mailbox_node") or "")
        message = f"seconds={seconds:.2f} node={node} body={text[:160].replace(chr(10), ' ')}"
        return CheckResult(f"probe:{url}", ok, seconds, status, message)
    except Exception as exc:
        return CheckResult(f"probe:{url}", False, time.time() - started, None, repr(exc))


def collect_container_log_alerts() -> tuple[dict[str, int], list[str]]:
    container = os.getenv("MAIL_CLUSTER_LOG_CONTAINER", "").strip()
    if not container:
        return {}, []
    window_seconds = max(60, env_int("MAIL_CLUSTER_LOG_WINDOW_SECONDS", 300))
    try:
        completed = subprocess.run(
            [
                "docker",
                "logs",
                "--since",
                f"{window_seconds}s",
                "--tail",
                "20000",
                container,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    except Exception as exc:
        return {}, [f"docker logs failed: {exc!r}"]

    counters = {
        "http429": len(re.findall(r"\b429\b", text)),
        "busy": len(
            re.findall(
                r"EMAIL_FETCH_THROTTLED|concurrency limit reached|邮箱服务当前繁忙",
                text,
                flags=re.IGNORECASE,
            )
        ),
        "http502": len(re.findall(r"\b502\b", text)),
        "http504": len(re.findall(r"\b504\b", text)),
        "timeout": len(
            re.findall(
                r"EMAIL_FETCH_TIMEOUT|PUBLIC_MAILBOX_FETCH_TIMEOUT|timed out|status=504|[\" ]504[\" ]|查询超时|upstream timeout",
                text,
                flags=re.IGNORECASE,
            )
        ),
        "go_client": len(re.findall(r"Go-http-client/2\.0", text)),
    }
    thresholds = {
        "http429": env_int("MAIL_CLUSTER_ALERT_429", 20),
        "busy": env_int("MAIL_CLUSTER_ALERT_BUSY", 20),
        "http502": env_int("MAIL_CLUSTER_ALERT_502", 5),
        "http504": env_int("MAIL_CLUSTER_ALERT_504", 5),
        "timeout": env_int("MAIL_CLUSTER_ALERT_TIMEOUT", 5),
        "go_client": env_int("MAIL_CLUSTER_ALERT_GO_CLIENT", 500),
    }
    alerts = [
        f"log threshold exceeded {name}={counters[name]} threshold={thresholds[name]}"
        for name in counters
        if counters[name] > thresholds[name]
    ]
    return counters, alerts


def notify(message: str) -> None:
    print(message, flush=True)
    webhook_url = os.getenv("MAIL_CLUSTER_ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        post_json(
            webhook_url,
            {
                "text": message,
                "service": "mail-cluster-monitor",
                "timestamp": int(time.time()),
            },
            timeout=5,
        )
    except Exception as exc:
        print(f"webhook failed: {exc!r}", file=sys.stderr, flush=True)


def ensure_upstream(
    active_nodes: dict[str, int],
    expected_nodes: dict[str, int] | None = None,
    current_nodes: dict[str, int] | None = None,
) -> list[str]:
    if not apisix_management_enabled():
        return []
    admin_url, upstream_id, headers = apisix_admin_context()
    if not upstream_id or not headers["X-API-KEY"]:
        return ["APISIX upstream enforcement skipped: missing APISIX_UPSTREAM_ID or APISIX_API_KEY"]
    url = f"{admin_url}/upstreams/{upstream_id}"
    alerts: list[str] = []
    if current_nodes is None:
        current_nodes, read_alerts = read_current_upstream_nodes()
        alerts.extend(read_alerts)
    if current_nodes is None:
        return alerts
    if current_nodes != active_nodes:
        put_status = put_json(url, upstream_payload(active_nodes), headers=headers, timeout=5)
        alerts.append(
            f"APISIX upstream nodes corrected status={put_status} "
            f"from={current_nodes} to={active_nodes}"
        )
        if expected_nodes:
            offline_nodes = sorted(set(expected_nodes) - set(active_nodes))
            if offline_nodes:
                alerts.append(f"APISIX upstream business-unhealthy nodes offline nodes={offline_nodes}")
    try:
        status, payload, _text = http_json(url, headers=headers, timeout=5)
    except Exception as exc:
        return alerts + [f"APISIX upstream re-read failed: {exc!r}"]
    if status != 200 or not isinstance(payload, dict):
        return alerts + [f"APISIX upstream re-read failed status={status}"]
    value = payload.get("value") or {}
    checks = value.get("checks") if isinstance(value, dict) else None
    active = checks.get("active") if isinstance(checks, dict) else None
    expected_path = os.getenv("MAIL_CLUSTER_APISIX_HEALTHCHECK_PATH", "/health/business").strip()
    if not expected_path.startswith("/"):
        expected_path = "/health/business"
    current_path = active.get("http_path") if isinstance(active, dict) else None
    if not isinstance(checks, dict) or "active" not in checks or current_path != expected_path:
        put_status = put_json(url, upstream_payload(active_nodes), headers=headers, timeout=5)
        alerts.append(f"APISIX upstream healthcheck restored status={put_status}")
    return alerts


def main() -> int:
    expected_nodes = load_expected_nodes()
    ready_timeout = env_float("MAIL_CLUSTER_READY_TIMEOUT_SECONDS", 4.0)
    node_probe_path = os.getenv("MAIL_CLUSTER_NODE_PROBE_PATH", "/health/business").strip() or "/health/business"
    probe_timeout = env_float("MAIL_CLUSTER_PROBE_TIMEOUT_SECONDS", 12.0)
    probe_slow_seconds = env_float("MAIL_CLUSTER_PROBE_SLOW_SECONDS", 5.0)
    failures_allowed = env_int("MAIL_CLUSTER_FAILURES_ALLOWED", 0)
    failure_threshold = max(1, env_int("MAIL_CLUSTER_FAILURES_TO_OFFLINE", 2))
    success_threshold = max(1, env_int("MAIL_CLUSTER_SUCCESSES_TO_ONLINE", 2))
    cooldown_seconds = max(
        0.0,
        min(env_float("MAIL_CLUSTER_NODE_COOLDOWN_SECONDS", 120.0), 3600.0),
    )

    node_results = [check_node(node, ready_timeout, node_probe_path) for node in expected_nodes]
    current_nodes, state_alerts = read_current_upstream_nodes()
    state = load_monitor_state(expected_nodes, current_nodes)
    active_nodes = update_monitor_state(
        state,
        node_results,
        weights=expected_nodes,
        failure_threshold=failure_threshold,
        success_threshold=success_threshold,
        cooldown_seconds=cooldown_seconds,
    )
    save_monitor_state(state)
    alerts = []
    alerts.extend(state_alerts)
    if active_nodes:
        alerts.extend(ensure_upstream(active_nodes, expected_nodes, current_nodes))
    else:
        alerts.append("APISIX upstream update skipped: all expected nodes failed business probe")
    results = list(node_results)

    raw_probes = os.getenv("MAIL_CLUSTER_PUBLIC_PROBES", "").strip()
    probes = [item.strip() for item in raw_probes.split(",") if item.strip()]
    results.extend(check_probe(url, probe_timeout, probe_slow_seconds) for url in probes)

    failures = [result for result in results if not result.ok]
    log_counts, log_alerts = collect_container_log_alerts()
    summary = {
        "ok": len(failures) <= failures_allowed and not log_alerts,
        "checked": len(results),
        "failures": len(failures),
        "log_counts": log_counts,
        "results": [
            {
                "name": result.name,
                "ok": result.ok,
                "status": result.status,
                "seconds": round(result.seconds, 3),
                "message": result.message,
            }
            for result in results
        ],
    }
    all_alerts = alerts + log_alerts
    if all_alerts or failures:
        notify("MAIL_CLUSTER_ALERT " + json.dumps({"alerts": all_alerts, **summary}, ensure_ascii=False))
    else:
        print("MAIL_CLUSTER_OK " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 2 if (failures and len(failures) > failures_allowed) or log_alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
