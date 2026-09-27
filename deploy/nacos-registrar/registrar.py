#!/usr/bin/env python3
import ipaddress
import json
import logging
import os
import signal
import threading
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("nacos-registrar")


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def positive(name, default):
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


class Config:
    def __init__(self):
        parsed = urllib.parse.urlsplit(required("NACOS_SERVER_URL").rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("NACOS_SERVER_URL is invalid")
        if parsed.path not in ("", "/nacos"):
            raise ValueError("NACOS_SERVER_URL path must be empty or /nacos")
        self.api_root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/nacos", "", ""))
        self.service_name = required("NACOS_SERVICE_NAME")
        address = ipaddress.ip_address(required("NACOS_ADVERTISE_IP"))
        if address.version != 4:
            raise ValueError("NACOS_ADVERTISE_IP must be IPv4")
        self.ip = str(address)
        self.port = int(required("NACOS_ADVERTISE_PORT"))
        if not 1 <= self.port <= 65535:
            raise ValueError("NACOS_ADVERTISE_PORT is out of range")
        self.namespace_id = os.environ.get("NACOS_NAMESPACE_ID", "public")
        self.group_name = os.environ.get("NACOS_GROUP_NAME", "DEFAULT_GROUP")
        self.cluster_name = os.environ.get("NACOS_CLUSTER_NAME", "DEFAULT")
        self.username = os.environ.get("NACOS_USERNAME", "")
        self.password = os.environ.get("NACOS_PASSWORD", "")
        if bool(self.username) != bool(self.password):
            raise ValueError("NACOS_USERNAME and NACOS_PASSWORD must be provided together")
        self.ready_url = required("REPLICA_READY_URL")
        self.heartbeat_interval = positive("NACOS_HEARTBEAT_INTERVAL", "5")
        self.readiness_interval = positive("NACOS_READINESS_INTERVAL", "2")
        self.retry_initial = positive("NACOS_RETRY_INITIAL", "1")
        self.retry_max = positive("NACOS_RETRY_MAX", "30")
        self.http_timeout = positive("NACOS_HTTP_TIMEOUT", "5")


class Registrar:
    def __init__(self, config):
        self.config = config
        self.token = None
        self.stop_event = threading.Event()
        self.registered = False

    def stop(self, *_):
        self.stop_event.set()

    def perform(self, method, path, params):
        url = f"{self.config.api_root}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if method in ("GET", "DELETE"):
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}" if query else url
        else:
            data = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.config.http_timeout) as response:
            return response.read()

    def login(self):
        body = self.perform(
            "POST",
            "/v1/auth/users/login",
            {"username": self.config.username, "password": self.config.password},
        )
        payload = json.loads(body.decode())
        self.token = payload.get("accessToken")
        if not self.token:
            raise RuntimeError("Nacos login response did not include accessToken")

    def request(self, method, path, params):
        for attempt in range(2):
            request_params = dict(params)
            if self.config.username:
                if not self.token:
                    self.login()
                request_params["accessToken"] = self.token
            try:
                return self.perform(method, path, request_params)
            except urllib.error.HTTPError as error:
                if error.code in (401, 403) and self.config.username and attempt == 0:
                    error.close()
                    self.token = None
                    continue
                raise
        raise RuntimeError("Nacos authentication retry was exhausted")

    def instance_params(self):
        return {
            "serviceName": self.config.service_name,
            "groupName": self.config.group_name,
            "clusterName": self.config.cluster_name,
            "namespaceId": self.config.namespace_id,
            "ip": self.config.ip,
            "port": str(self.config.port),
            "ephemeral": "true",
        }

    def enabled_instance_params(self):
        params = self.instance_params()
        params.update(weight="1.0", enabled="true", healthy="true")
        return params

    def register(self):
        # Remove only this instance first so a stale disabled flag cannot survive re-registration.
        try:
            self.request("DELETE", "/v1/ns/instance", self.instance_params())
        except Exception as error:
            LOG.warning("Pre-registration cleanup failed (%s)", type(error).__name__)
        params = self.enabled_instance_params()
        self.request("POST", "/v1/ns/instance", params)
        self.request("PUT", "/v1/ns/instance", params)
        self.registered = True
        LOG.info("Registered %s at %s:%s", self.config.service_name, self.config.ip, self.config.port)

    def heartbeat(self):
        beat = {
            "serviceName": self.config.service_name,
            "groupName": self.config.group_name,
            "clusterName": self.config.cluster_name,
            "cluster": self.config.cluster_name,
            "ip": self.config.ip,
            "port": self.config.port,
            "weight": 1.0,
            "enabled": True,
            "healthy": True,
            "ephemeral": True,
        }
        body = self.request(
            "PUT",
            "/v1/ns/instance/beat",
            {
                "serviceName": self.config.service_name,
                "groupName": self.config.group_name,
                "namespaceId": self.config.namespace_id,
                "ephemeral": "true",
                "beat": json.dumps(beat, separators=(",", ":")),
            },
        )
        try:
            payload = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and str(payload.get("code")) == "20404":
            self.registered = False
            self.register()

    def deregister(self):
        self.request("DELETE", "/v1/ns/instance", self.instance_params())
        self.registered = False
        LOG.info(
            "Deregistered %s at %s:%s",
            self.config.service_name,
            self.config.ip,
            self.config.port,
        )

    def ready(self):
        try:
            request = urllib.request.Request(self.config.ready_url, method="GET")
            with urllib.request.urlopen(request, timeout=self.config.http_timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError):
            return False

    def run(self):
        retry = self.config.retry_initial
        try:
            while not self.stop_event.is_set():
                if not self.ready():
                    if self.registered:
                        try:
                            self.deregister()
                            retry = self.config.retry_initial
                        except Exception:
                            self.stop_event.wait(retry)
                            retry = min(retry * 2, self.config.retry_max)
                            continue
                    self.stop_event.wait(self.config.readiness_interval)
                    continue
                if not self.registered:
                    try:
                        self.register()
                        retry = self.config.retry_initial
                    except Exception as error:
                        LOG.warning("Registration failed (%s); retrying", type(error).__name__)
                        self.stop_event.wait(retry)
                        retry = min(retry * 2, self.config.retry_max)
                        continue
                try:
                    self.heartbeat()
                    retry = self.config.retry_initial
                    self.stop_event.wait(self.config.heartbeat_interval)
                except Exception as error:
                    LOG.warning("Heartbeat failed (%s); retrying", type(error).__name__)
                    self.stop_event.wait(retry)
                    retry = min(retry * 2, self.config.retry_max)
        finally:
            if self.registered:
                try:
                    self.deregister()
                except Exception as error:
                    LOG.warning("Final deregistration failed (%s)", type(error).__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    registrar = Registrar(Config())
    signal.signal(signal.SIGTERM, registrar.stop)
    signal.signal(signal.SIGINT, registrar.stop)
    registrar.run()


if __name__ == "__main__":
    main()
