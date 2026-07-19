from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DISCORD_WEBHOOK_HOSTS = {
    "discord.com",
    "ptb.discord.com",
    "canary.discord.com",
    "discordapp.com",
}
DISCORD_WEBHOOK_PATH_RE = re.compile(
    r"^/api(?:/v\d+)?/webhooks/(?P<webhook_id>\d{17,20})/(?P<token>[A-Za-z0-9._-]{20,})/?$"
)


def normalize_webhook_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise ValueError("올바른 Discord 웹훅 URL을 입력해주세요.") from error

    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in DISCORD_WEBHOOK_HOSTS:
        raise ValueError("Discord 공식 HTTPS 웹훅 URL만 사용할 수 있습니다.")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("올바른 Discord 웹훅 URL을 입력해주세요.")
    if not DISCORD_WEBHOOK_PATH_RE.fullmatch(parsed.path):
        raise ValueError("Discord 채널에서 생성한 웹훅 URL을 입력해주세요.")

    return urlunsplit(("https", hostname, parsed.path.rstrip("/"), parsed.query, ""))


def masked_webhook_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    matched = DISCORD_WEBHOOK_PATH_RE.fullmatch(parsed.path)
    if not matched:
        return "등록된 웹훅"
    webhook_id = matched.group("webhook_id")
    return f"Discord 웹훅 · {webhook_id[:4]}...{webhook_id[-4:]}"


def webhook_execute_url(value: str) -> str:
    parsed = urlsplit(normalize_webhook_url(value))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def build_webhook_payload(
    *,
    username: str,
    title: str,
    description: str,
    color: int,
    fields: list[dict[str, Any]] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    safe_fields = []
    for field in (fields or [])[:25]:
        name = str(field.get("name") or "-")[:256]
        value = str(field.get("value") or "-")[:1024]
        safe_fields.append({"name": name, "value": value, "inline": bool(field.get("inline", True))})

    sent_at = timestamp or datetime.now(timezone.utc)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return {
        "username": str(username or "TechTim Minecraft Server")[:80],
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": str(title or "Minecraft 서버 알림")[:256],
            "description": str(description or "서버 상태가 변경되었습니다.")[:4096],
            "color": max(0, min(0xFFFFFF, int(color))),
            "fields": safe_fields,
            "footer": {"text": "TechTim Minecraft Server Panel"},
            "timestamp": sent_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        }],
    }


def execute_webhook(url: str, payload: dict[str, Any], timeout: float = 8.0) -> None:
    request = Request(
        webhook_execute_url(url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "TechTim-Minecraft-Panel/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status not in {200, 204}:
                raise RuntimeError(f"Discord가 HTTP {response.status} 응답을 반환했습니다.")
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8")).get("message")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            details = None
        message = str(details or f"HTTP {error.code}")
        raise RuntimeError(f"Discord 웹훅 전송에 실패했습니다: {message}") from error
    except URLError as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"Discord 웹훅에 연결하지 못했습니다: {reason}") from error

