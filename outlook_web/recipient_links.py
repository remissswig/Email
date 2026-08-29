from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any
from urllib.parse import SplitResult, urlsplit


MAX_RECIPIENT_LINE_LENGTH = 320

_INVALID_EXPORT_STEM_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOCAL_PART_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9-]+$")
_URL_PATH_RE = re.compile(r"^(?:/(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})*)*$")


class RecipientLinkInputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RecipientAddress:
    display: str
    normalized: str


@dataclass(frozen=True)
class ParsedRecipientFile:
    recipients: list[RecipientAddress]
    errors: list[dict[str, object]]


def normalize_recipient_email(value: Any) -> RecipientAddress:
    display = str(value or "").strip()
    if not display or len(display) > MAX_RECIPIENT_LINE_LENGTH:
        raise RecipientLinkInputError("invalid_email")
    if any(char.isspace() for char in display):
        raise RecipientLinkInputError("invalid_email")
    if display.count("@") != 1:
        raise RecipientLinkInputError("invalid_email")

    local_part, domain = display.split("@", 1)
    if not local_part or not domain:
        raise RecipientLinkInputError("invalid_email")
    if not _LOCAL_PART_RE.fullmatch(local_part):
        raise RecipientLinkInputError("invalid_email")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise RecipientLinkInputError("invalid_email")
    if len(domain) > 253:
        raise RecipientLinkInputError("invalid_email")
    for label in domain.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not _DOMAIN_LABEL_RE.fullmatch(label)
        ):
            raise RecipientLinkInputError("invalid_email")

    try:
        parsed = getaddresses([display], strict=True)
    except TypeError:
        parsed = getaddresses([display])
    if len(parsed) != 1 or parsed[0][0] or parsed[0][1] != display:
        raise RecipientLinkInputError("invalid_email")

    return RecipientAddress(display=display, normalized=display.lower())


def _utc_rfc3339(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_expiry(choice: Any, raw: Any, now: datetime) -> str | None:
    normalized_choice = str(choice or "").strip().lower() or "never"
    now_utc = now.astimezone(timezone.utc)

    if normalized_choice in {"never", "permanent"}:
        return None
    if normalized_choice == "1d":
        return _utc_rfc3339(now_utc + timedelta(days=1))
    if normalized_choice == "7d":
        return _utc_rfc3339(now_utc + timedelta(days=7))
    if normalized_choice == "30d":
        return _utc_rfc3339(now_utc + timedelta(days=30))
    if normalized_choice != "custom":
        raise RecipientLinkInputError("invalid_expiry")

    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception as exc:
        raise RecipientLinkInputError("invalid_expires_at") from exc

    if parsed.tzinfo is None:
        raise RecipientLinkInputError("expires_at_timezone_required")

    return _utc_rfc3339(parsed)


def normalize_public_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RecipientLinkInputError("invalid_public_base_url")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise RecipientLinkInputError("invalid_public_base_url")
    if "\\" in raw:
        raise RecipientLinkInputError("invalid_public_base_url")

    trimmed = raw.rstrip("/")
    try:
        parsed = urlsplit(trimmed)
        _ = parsed.port
    except Exception as exc:
        raise RecipientLinkInputError("invalid_public_base_url") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise RecipientLinkInputError("invalid_public_base_url")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RecipientLinkInputError("invalid_public_base_url")
    if parsed.query or parsed.fragment:
        raise RecipientLinkInputError("invalid_public_base_url")
    if not _URL_PATH_RE.fullmatch(parsed.path):
        raise RecipientLinkInputError("invalid_public_base_url")

    normalized_path = parsed.path.rstrip("/")
    normalized = parsed._replace(scheme=scheme, path=normalized_path)
    return SplitResult(*normalized).geturl()


def _recipient_email_from_import_line(line: str) -> str:
    if "----" not in line:
        return line

    parts = [part.strip() for part in line.split("----")]
    for part in parts:
        if not part:
            continue
        try:
            normalize_recipient_email(part)
        except RecipientLinkInputError:
            continue
        return part

    return parts[0] if parts else line


def _parse_recipient_txt_lines(lines: list[tuple[int, str]]) -> ParsedRecipientFile:
    recipients: list[RecipientAddress] = []
    errors: list[dict[str, object]] = []
    seen_normalized: set[str] = set()

    for line_number, raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        recipient_value = _recipient_email_from_import_line(line)
        if len(recipient_value) > MAX_RECIPIENT_LINE_LENGTH:
            errors.append(
                {
                    "line": line_number,
                    "value": recipient_value[:MAX_RECIPIENT_LINE_LENGTH],
                    "error_code": "line_too_long",
                }
            )
            continue

        try:
            recipient = normalize_recipient_email(recipient_value)
        except RecipientLinkInputError as exc:
            errors.append(
                {"line": line_number, "value": recipient_value, "error_code": exc.code}
            )
            continue

        if recipient.normalized in seen_normalized:
            continue

        seen_normalized.add(recipient.normalized)
        recipients.append(recipient)

    return ParsedRecipientFile(recipients=recipients, errors=errors)


def decode_recipient_txt(content: bytes) -> ParsedRecipientFile:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RecipientLinkInputError("invalid_utf8") from exc

    return _parse_recipient_txt_lines(list(enumerate(decoded.splitlines(), start=1)))


def decode_recipient_txt_with_main_email(content: bytes) -> tuple[str, ParsedRecipientFile]:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RecipientLinkInputError("invalid_utf8") from exc

    main_email = ""
    recipient_lines: list[tuple[int, str]] = []
    found_main_email = False
    for line_number, raw_line in enumerate(decoded.splitlines(), start=1):
        line = raw_line.strip()
        if not found_main_email:
            if not line:
                continue
            main_email = _recipient_email_from_import_line(line)
            found_main_email = True
            continue
        recipient_lines.append((line_number, raw_line))

    return main_email, _parse_recipient_txt_lines(recipient_lines)


def safe_export_stem(value: Any) -> str:
    stem = _INVALID_EXPORT_STEM_RE.sub("_", str(value or "")).strip(". ")
    return stem or "verification-links"
