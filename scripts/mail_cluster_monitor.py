#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request


DEFAULT_NODES = {
    "172.17.0.1:5000": 2,
    "68.221.72.212:5000": 1,
    "91.233.10.96:5005": 1,
    "172.93.219.182:5000": 1,
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


def upstream_payload(expected_nodes: dict[str, int]) -> dict[str, Any]:
    return {
        "name": os.getenv("MAIL_CLUSTER_UPSTREAM_NAME", "mail-cluster"),
        "desc": os.getenv(
            "MAIL_CLUSTER_UPSTREAM_DESC",
            "mail-cluster active nodes; managed healthcheck enabled",
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
                "http_path": "/health/ready",
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


def check_ready(node: str, timeout: float) -> CheckResult:
    url = f"http://{node}/health/ready"
    started = time.time()
    try:
        status, payload, text = http_json(url, timeout=timeout)
        seconds = time.time() - started
        ok = status == 200 and isinstance(payload, dict) and payload.get("success") is True
        message = text[:200].replace("\n", " ")
        return CheckResult(f"ready:{node}", ok, seconds, status, message)
    except Exception as exc:
        return CheckResult(f"ready:{node}", False, time.time() - started, None, repr(exc))


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


def ensure_upstream(expected_nodes: dict[str, int]) -> list[str]:
    if os.getenv("MAIL_CLUSTER_APISIX_MANAGE", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return []
    admin_url = os.getenv("APISIX_ADMIN_URL", "http://127.0.0.1:9180/apisix/admin").rstrip("/")
    upstream_id = os.getenv("APISIX_UPSTREAM_ID", "00000000000000000015").strip()
    api_key = os.getenv("APISIX_API_KEY", "").strip()
    if not upstream_id or not api_key:
        return ["APISIX upstream enforcement skipped: missing APISIX_UPSTREAM_ID or APISIX_API_KEY"]
    headers = {"X-API-KEY": api_key}
    url = f"{admin_url}/upstreams/{upstream_id}"
    try:
        status, payload, _text = http_json(url, headers=headers, timeout=5)
    except Exception as exc:
        return [f"APISIX upstream read failed: {exc!r}"]
    if status != 200 or not isinstance(payload, dict):
        return [f"APISIX upstream read failed status={status}"]
    current = dict((payload.get("value") or {}).get("nodes") or {})
    alerts = []
    if current != expected_nodes:
        put_status = put_json(url, upstream_payload(expected_nodes), headers=headers, timeout=5)
        alerts.append(f"APISIX upstream nodes corrected status={put_status} from={current} to={expected_nodes}")
    value = payload.get("value") or {}
    checks = value.get("checks") if isinstance(value, dict) else None
    if not isinstance(checks, dict) or "active" not in checks:
        put_status = put_json(url, upstream_payload(expected_nodes), headers=headers, timeout=5)
        alerts.append(f"APISIX upstream healthcheck restored status={put_status}")
    return alerts


def main() -> int:
    expected_nodes = load_expected_nodes()
    ready_timeout = env_float("MAIL_CLUSTER_READY_TIMEOUT_SECONDS", 4.0)
    probe_timeout = env_float("MAIL_CLUSTER_PROBE_TIMEOUT_SECONDS", 12.0)
    probe_slow_seconds = env_float("MAIL_CLUSTER_PROBE_SLOW_SECONDS", 5.0)
    failures_allowed = env_int("MAIL_CLUSTER_FAILURES_ALLOWED", 0)

    alerts = ensure_upstream(expected_nodes)
    results = [check_ready(node, ready_timeout) for node in expected_nodes]

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
