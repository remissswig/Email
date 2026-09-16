#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


NACOS_URL = os.getenv("MAIL_CLUSTER_NACOS_URL", "http://127.0.0.1:8848").rstrip("/")
SERVICE_NAME = os.getenv("MAIL_CLUSTER_SERVICE_NAME", "mail-cluster")
HOST_HEADER = os.getenv("MAIL_CLUSTER_HOST_HEADER", "mail.example.com")
PROBE_PATH = os.getenv("MAIL_CLUSTER_PROBE_PATH", "").strip()
STATE_PATH = Path(os.getenv("MAIL_CLUSTER_HEALTH_STATE", "/var/lib/mail-cluster-healthcheck/state.json"))
LOG_PATH = Path(os.getenv("MAIL_CLUSTER_HEALTH_LOG", "/var/log/mail-cluster-healthcheck.log"))
TIMEOUT_SECONDS = float(os.getenv("MAIL_CLUSTER_PROBE_TIMEOUT", "18"))
FAIL_THRESHOLD = max(1, int(os.getenv("MAIL_CLUSTER_FAIL_THRESHOLD", "2")))
RECOVER_THRESHOLD = max(1, int(os.getenv("MAIL_CLUSTER_RECOVER_THRESHOLD", "1")))


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp_path.replace(STATE_PATH)


def request_json(url: str, *, method: str = "GET", data: dict | None = None, headers: dict | None = None) -> dict:
    encoded = None
    if data is not None:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


def list_instances() -> list[dict]:
    url = f"{NACOS_URL}/nacos/v1/ns/instance/list?{urllib.parse.urlencode({'serviceName': SERVICE_NAME})}"
    payload = request_json(url)
    return list(payload.get("hosts") or [])


def instance_key(instance: dict) -> str:
    return f"{instance.get('ip')}:{instance.get('port')}"


def probe_instance(instance: dict) -> dict:
    url = f"http://{instance.get('ip')}:{instance.get('port')}{PROBE_PATH}"
    request = urllib.request.Request(url, headers={"Host": HOST_HEADER, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        status = int(response.status)
        body = response.read()
    if status != 200:
        return {"ok": False, "error": f"status={status}"}
    payload = json.loads(body.decode("utf-8"))
    if not payload.get("success"):
        return {"ok": False, "error": str(payload.get("error") or "probe_failed")}
    messages = list(payload.get("messages") or [])
    latest = str(messages[0].get("received_at") or "") if messages else ""
    return {
        "ok": True,
        "count": int(payload.get("count") or len(messages)),
        "latest": latest,
        "signature": f"{int(payload.get('count') or len(messages))}|{latest}",
    }


def set_instance_enabled(instance: dict, enabled: bool) -> None:
    data = {
        "serviceName": SERVICE_NAME,
        "ip": str(instance.get("ip") or ""),
        "port": str(instance.get("port") or ""),
        "enabled": "true" if enabled else "false",
        "weight": str(instance.get("weight") or 1.0),
        "ephemeral": "true" if instance.get("ephemeral", True) else "false",
    }
    request_json(f"{NACOS_URL}/nacos/v1/ns/instance", method="PUT", data=data)


def majority_signature(results: dict[str, dict]) -> str:
    counts: dict[str, int] = {}
    for result in results.values():
        if result.get("ok"):
            signature = str(result.get("signature") or "")
            counts[signature] = counts.get(signature, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return ""
    return ordered[0][0]


def main() -> int:
    if not PROBE_PATH.startswith("/"):
        log("skip missing MAIL_CLUSTER_PROBE_PATH")
        return 0

    state = load_state()
    instances = list_instances()
    if not instances:
        log("skip no nacos instances")
        return 1

    results: dict[str, dict] = {}
    instance_map = {instance_key(instance): instance for instance in instances}
    for key, instance in instance_map.items():
        try:
            results[key] = probe_instance(instance)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            results[key] = {"ok": False, "error": type(exc).__name__}

    majority = majority_signature(results)
    if not majority:
        log(f"skip no stable majority results={results}")
        save_state(state)
        return 1

    changed = []
    for key, instance in instance_map.items():
        result = results[key]
        enabled = bool(instance.get("enabled", True))
        healthy = bool(instance.get("healthy", True))
        is_good = bool(result.get("ok")) and result.get("signature") == majority
        record = state.setdefault(key, {"fail": 0, "recover": 0})
        if is_good:
            record["fail"] = 0
            record["recover"] = int(record.get("recover") or 0) + 1
            if not enabled and healthy and record["recover"] >= RECOVER_THRESHOLD:
                set_instance_enabled(instance, True)
                changed.append(f"enabled {key}")
        else:
            record["recover"] = 0
            record["fail"] = int(record.get("fail") or 0) + 1
            if enabled and record["fail"] >= FAIL_THRESHOLD:
                set_instance_enabled(instance, False)
                changed.append(f"disabled {key}: {result.get('error') or result.get('signature')}")

    save_state(state)
    log(f"majority={majority} results={results} changes={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
