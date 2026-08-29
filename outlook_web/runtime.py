#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime helpers for local execution and packaged builds."""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
import traceback
from pathlib import Path
from typing import Any


APP_NAME = "OutlookEmail"
SECRET_KEY_FILE = "secret_key.txt"
DATABASE_FILE = "outlook_accounts.db"
STARTUP_LOG_FILE = "startup-error.log"
SECRET_KEY_SOURCE = "unresolved"
SECRET_KEY_SOURCE_PATH = ""
SECRET_KEY_FINGERPRINT = ""
SECRET_KEY_FILE_EXISTS = False
SECRET_KEY_FILE_MATCHES: bool | None = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def runtime_root() -> Path:
    override = os.getenv("OUTLOOK_EMAIL_HOME")
    if override:
        root = Path(override).expanduser()
    elif is_frozen():
        if os.name == "nt":
            root = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / APP_NAME
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support" / APP_NAME
        else:
            xdg_home = os.getenv("XDG_DATA_HOME")
            root = Path(xdg_home).expanduser() / APP_NAME if xdg_home else Path.home() / ".local" / "share" / APP_NAME
    else:
        root = bundle_root()

    root.mkdir(parents=True, exist_ok=True)
    return root


def resource_path(*parts: str) -> Path:
    return bundle_root().joinpath(*parts)


def default_database_path() -> Path:
    if is_frozen():
        return runtime_root() / "data" / DATABASE_FILE
    return bundle_root() / "data" / DATABASE_FILE


def startup_log_path() -> Path:
    return runtime_root() / STARTUP_LOG_FILE


def _fingerprint_secret(secret_key: str | None) -> str:
    if not secret_key:
        return ""
    return hashlib.sha256(secret_key.encode("utf-8")).hexdigest()[:12]


def _set_secret_key_diagnostics(
    source: str,
    secret_key: str | None,
    path: Path | None = None,
    file_exists: bool = False,
    file_matches: bool | None = None,
) -> None:
    global SECRET_KEY_SOURCE, SECRET_KEY_SOURCE_PATH, SECRET_KEY_FINGERPRINT
    global SECRET_KEY_FILE_EXISTS, SECRET_KEY_FILE_MATCHES

    SECRET_KEY_SOURCE = source
    SECRET_KEY_SOURCE_PATH = str(path or "")
    SECRET_KEY_FINGERPRINT = _fingerprint_secret(secret_key)
    SECRET_KEY_FILE_EXISTS = file_exists
    SECRET_KEY_FILE_MATCHES = file_matches


def secret_key_diagnostics() -> dict[str, Any]:
    return {
        "source": SECRET_KEY_SOURCE,
        "path": SECRET_KEY_SOURCE_PATH,
        "fingerprint": SECRET_KEY_FINGERPRINT,
        "file_exists": SECRET_KEY_FILE_EXISTS,
        "file_matches": SECRET_KEY_FILE_MATCHES,
    }


def resolve_secret_key() -> str | None:
    secret_key = os.getenv("SECRET_KEY")
    if secret_key:
        secret_key_path = runtime_root() / SECRET_KEY_FILE
        if secret_key_path.exists():
            stored = secret_key_path.read_text(encoding="utf-8").strip()
            file_matches = stored == secret_key
            source = "environment+file" if file_matches else "environment"
            _set_secret_key_diagnostics(source, secret_key, secret_key_path, True, file_matches)
        else:
            _set_secret_key_diagnostics("environment", secret_key, None, False, None)
        return secret_key

    if not is_frozen():
        _set_secret_key_diagnostics("missing", None, None, False, None)
        return None

    secret_key_path = runtime_root() / SECRET_KEY_FILE
    if secret_key_path.exists():
        stored = secret_key_path.read_text(encoding="utf-8").strip()
        if stored:
            _set_secret_key_diagnostics("file", stored, secret_key_path, True, True)
            return stored

    generated = secrets.token_hex(32)
    secret_key_path.write_text(generated, encoding="utf-8")
    _set_secret_key_diagnostics("generated-file", generated, secret_key_path, True, True)
    return generated


def record_startup_error(exc: BaseException) -> Path:
    log_path = startup_log_path()
    error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path.write_text(error_text, encoding="utf-8")
    return log_path


def notify_startup_error(log_path: Path) -> None:
    message = (
        "OutlookEmail 启动失败。\n\n"
        f"错误日志已写入:\n{log_path}\n\n"
        "请把这个日志发给开发者。"
    )

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "OutlookEmail", 0x10)
            return
        except Exception:
            pass

    print(message, file=sys.stderr)
