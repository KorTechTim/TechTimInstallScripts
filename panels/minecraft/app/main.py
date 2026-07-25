from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request as UrlRequest, urlopen
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import tarfile
import threading
import time
import zipfile

import docker
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.game_metrics import (
    container_uptime_seconds,
    health_state,
    parse_forge_tps,
    parse_paper_mspt,
    parse_paper_tps,
    parse_player_counts,
    parse_spark_tps,
    parse_tick_query,
    version_at_least,
)
from app.discord_webhook import (
    build_webhook_payload,
    execute_webhook,
    masked_webhook_url,
    normalize_webhook_url,
)


app = FastAPI(title="TechTim Minecraft Server Panel")
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

PANEL_VERSION = os.getenv("PANEL_VERSION", "1.0.0")
STATIC_ASSET_VERSION = hashlib.sha256(
    (APP_DIR / "static" / "app.css").read_bytes()
    + (APP_DIR / "static" / "panel-overrides.css").read_bytes()
    + (APP_DIR / "static" / "app.js").read_bytes()
).hexdigest()[:12]
MINECRAFT_VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/minecraft/data"))
SERVER_DIR = DATA_DIR / "server"
HOST_SERVER_DIR = HOST_DATA_DIR / "server"
SERVER_ICON_FILE = SERVER_DIR / "server-icon.png"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
DOWNLOAD_EXPORT_DIR = DATA_DIR / ".downloads"
SERVER_CONTAINER = os.getenv("MINECRAFT_SERVER_CONTAINER", "minecraft-server")
RUNTIME_IMAGE = os.getenv("MINECRAFT_RUNTIME_IMAGE", "itzg/minecraft-server:latest")
RUNTIME_CONFIG_LABEL = "kr.techtim.minecraft.runtime-config"
PANEL_CONTAINER_NAME = os.getenv("PANEL_CONTAINER_NAME", "minecraft-panel")
PANEL_PROXY_CONTAINER = os.getenv("PANEL_PROXY_CONTAINER", "minecraft-panel-proxy")
PANEL_IMAGE = os.getenv("PANEL_IMAGE", "ghcr.io/kortechtim/minecraft-panel:latest")
SERVER_PORT = int(os.getenv("SERVER_PORT", "25565"))
PULL_HEARTBEAT_SECONDS = max(2, int(os.getenv("DOCKER_PULL_HEARTBEAT_SECONDS", "10")))

AUTH_FILE = DATA_DIR / "auth.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
CONFIG_FILE = DATA_DIR / "minecraft-config.json"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"
INSTALL_MARKER_FILE = DATA_DIR / "install-request.txt"
CONTROL_LOG_FILE = DATA_DIR / "server-control.log"
BACKUP_CONFIG_FILE = DATA_DIR / "backup-config.json"
BACKUP_STATUS_FILE = DATA_DIR / "backup-status.json"
RESTART_SCHEDULE_FILE = DATA_DIR / "restart-schedule.json"
DISCORD_CONFIG_FILE = DATA_DIR / "discord-config.json"
PANEL_UPDATE_STATUS_FILE = DATA_DIR / "panel-update-status.json"
SESSION_COOKIE = "techtim_session"
INSTALL_LOCK = threading.Lock()
INSTALL_ACTIVE = False
SERVER_STDIN_LOCK = threading.Lock()
MAINTENANCE_LOCK = threading.Lock()
BACKUP_LOCK = MAINTENANCE_LOCK
BACKUP_ACTIVE = False
RESTART_SCHEDULE_LOCK = threading.Lock()
RESTART_OPERATION_LOCK = MAINTENANCE_LOCK
RESTART_OPERATION_ACTIVE = False
RESTART_SCHEDULER_STOP = threading.Event()
RESTART_SCHEDULER_THREAD: threading.Thread | None = None
RESTART_SCHEDULER_THREAD_LOCK = threading.Lock()
DISCORD_CONFIG_LOCK = threading.Lock()
DISCORD_MONITOR_STOP = threading.Event()
DISCORD_MONITOR_THREAD: threading.Thread | None = None
DISCORD_MONITOR_THREAD_LOCK = threading.Lock()
DISCORD_RUNTIME_STATE_LOCK = threading.Lock()
DISCORD_RUNTIME_STATE: dict[str, Any] = {}
DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL = 0.0
DISCORD_RUNTIME_LAST_ALERT_AT = 0.0
PANEL_UPDATE_LOCK = threading.Lock()
PANEL_UPDATE_ACTIVE = False
PANEL_UPDATE_CHECK_LOCK = threading.Lock()
PANEL_UPDATE_CHECK_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
VERSION_CACHE_LOCK = threading.Lock()
VERSION_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
RESOURCE_LOCK = threading.Lock()
RESOURCE_NETWORK_SAMPLE: dict[str, Any] = {
    "container_id": "",
    "sampled_at": 0.0,
    "received": 0,
    "sent": 0,
}
OS_CPU_LOCK = threading.Lock()
OS_CPU_SAMPLE: dict[int, tuple[int, int]] = {}
PROC_STAT_FILE = Path(os.getenv("PROC_STAT_FILE", "/proc/stat"))
PROC_MEMINFO_FILE = Path(os.getenv("PROC_MEMINFO_FILE", "/proc/meminfo"))
PUBLIC_IP_LOCK = threading.Lock()
PUBLIC_IP_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": ""}
GAME_METRICS_LOCK = threading.Lock()
GAME_METRICS_CACHE_SECONDS = max(5, int(os.getenv("GAME_METRICS_CACHE_SECONDS", "5")))
GAME_METRICS_CACHE: dict[str, Any] = {"container_id": "", "expires_at": 0.0, "payload": None}
KST = timezone(timedelta(hours=9), name="KST")
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_FRAGMENT_RE = re.compile(r"(?:\[(?:0|1|2|3|4|5|7|9|10[0-7]|[34][0-7])m)+")
RCON_TRANSPORT_NOISE_RE = re.compile(r"\bThread RCON Client\b.*\b(?:started|shutting down)\s*$", re.IGNORECASE)

SERVER_TYPES = {
    "VANILLA", "PAPER", "PURPUR", "SPIGOT", "FORGE", "NEOFORGE",
    "FABRIC", "QUILT",
}
MODPACK_URL_TYPES = {"FORGE", "NEOFORGE", "FABRIC"}
JAVA_VERSIONS = {"AUTO", "8", "11", "16", "17", "21", "25"}
STOPPABLE_SERVER_STATUSES = {"running", "restarting", "paused"}
DISCORD_EVENT_SETTINGS = {
    "server_start": "notify_server_start",
    "server_stop": "notify_server_stop",
    "server_restart": "notify_server_restart",
    "backup": "notify_backup",
    "error": "notify_errors",
}
DISCORD_EVENT_COLORS = {
    "server_start": 0x57A65A,
    "server_stop": 0x9A594D,
    "server_restart": 0xD29A45,
    "backup": 0x4B8495,
    "error": 0xC34B43,
    "test": 0x5865F2,
}

FALLBACK_MINECRAFT_VERSIONS = [
    "26.2", "26.1.2", "26.1.1", "26.1",
    "1.21.11", "1.21.10", "1.21.9", "1.21.8", "1.21.7", "1.21.6",
    "1.21.5", "1.21.4", "1.21.3", "1.21.2", "1.21.1", "1.21",
    "1.20.6", "1.20.5", "1.20.4", "1.20.3", "1.20.2", "1.20.1", "1.20",
    "1.19.4", "1.19.3", "1.19.2", "1.19.1", "1.19",
    "1.18.2", "1.18.1", "1.18",
    "1.17.1", "1.17",
    "1.16.5", "1.16.4", "1.16.3", "1.16.2", "1.16.1", "1.16",
    "1.15.2", "1.15.1", "1.15",
    "1.14.4", "1.14.3", "1.14.2", "1.14.1", "1.14",
    "1.13.2", "1.13.1", "1.13",
    "1.12.2", "1.12.1",
]


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    new_password: str


class ServerCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)


class PlayerActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    player: str = Field(min_length=1, max_length=16)
    reason: str = Field(default="", max_length=120)


class StartServerRequest(BaseModel):
    eula_accepted: bool = False


class BackupConfigRequest(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=6, ge=1, le=168)
    retention_count: int = Field(default=7, ge=1, le=50)


class RestartScheduleRequest(BaseModel):
    enabled: bool = False
    restart_time: str = Field(default="04:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class DiscordConfigRequest(BaseModel):
    enabled: bool = False
    webhook_url: str = Field(default="", max_length=500)
    clear_webhook: bool = False
    username: str = Field(default="TechTim Minecraft Server", min_length=1, max_length=80)
    notify_server_start: bool = True
    notify_server_stop: bool = True
    notify_server_restart: bool = True
    notify_backup: bool = True
    notify_errors: bool = True


class ConfigRequest(BaseModel):
    Type: str = "PAPER"
    Version: str = "LATEST"
    JavaVersion: str = "AUTO"
    Memory: str = "4G"
    ServerName: str = "TechTim Minecraft Server"
    Motd: str = Field(default="TechTim Minecraft Server", max_length=300)
    Level: str = "world"
    Seed: str = ""
    Difficulty: str = "normal"
    GameMode: str = "survival"
    MaxPlayers: int = 20
    OnlineMode: bool = True
    Pvp: bool = True
    AllowFlight: bool = False
    EnableCommandBlock: bool = False
    ViewDistance: int = 10
    SimulationDistance: int = 10
    SpawnProtection: int = 16
    Whitelist: str = ""
    Ops: str = ""
    ModpackUrl: str = ""


class CreateDirRequest(BaseModel):
    path: str = ""
    name: str


class DeleteRequest(BaseModel):
    path: str


class FileDownloadRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def clean_log(value: str) -> str:
    text = ANSI_RE.sub("", str(value or ""))
    text = ANSI_FRAGMENT_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line for line in text.split("\n") if not RCON_TRANSPORT_NOISE_RE.search(line))


def append_log(path: Path, message: str) -> None:
    ensure_dirs()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {clean_log(message)}\n")


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def initialize_auth() -> None:
    ensure_dirs()
    if not AUTH_FILE.exists():
        salt = secrets.token_hex(16)
        AUTH_FILE.write_text(json.dumps({
            "username": "admin",
            "password_hash": hash_password("admin", salt),
            "salt": salt,
            "must_change_password": True,
        }, indent=2), encoding="utf-8")
    if not SESSIONS_FILE.exists():
        SESSIONS_FILE.write_text("{}", encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def current_user(request: Request) -> str | None:
    initialize_auth()
    session = read_json(SESSIONS_FILE, {}).get(request.cookies.get(SESSION_COOKIE, ""))
    return session.get("username") if session else None


def require_auth(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def default_config() -> dict:
    return ConfigRequest().model_dump()


def releases_since_1_12_1(manifest: dict) -> list[str]:
    releases = []
    for version in manifest.get("versions") or []:
        if not isinstance(version, dict) or version.get("type") != "release":
            continue
        version_id = str(version.get("id") or "").strip()
        if not version_id or version_id in releases:
            continue
        releases.append(version_id)
        if version_id == "1.12.1":
            return releases
    return FALLBACK_MINECRAFT_VERSIONS.copy()


def minecraft_version_payload() -> dict:
    now = time.monotonic()
    with VERSION_CACHE_LOCK:
        cached = VERSION_CACHE.get("payload")
        if cached and float(VERSION_CACHE.get("expires_at") or 0) > now:
            return cached

    source = "mojang"
    try:
        with urlopen(MINECRAFT_VERSION_MANIFEST_URL, timeout=5) as response:
            manifest = json.load(response)
        releases = releases_since_1_12_1(manifest)
        latest = str((manifest.get("latest") or {}).get("release") or releases[0])
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        source = "fallback"
        releases = FALLBACK_MINECRAFT_VERSIONS.copy()
        latest = releases[0]

    payload = {
        "latest": latest,
        "versions": ["LATEST", *releases],
        "source": source,
    }
    with VERSION_CACHE_LOCK:
        VERSION_CACHE.update({"expires_at": now + 3600, "payload": payload})
    return payload


def normalize_config(raw: dict) -> dict:
    merged = {**default_config(), **(raw or {})}
    legacy_curseforge_selection = str(merged.get("ModpackSource") or "").strip().lower() == "curseforge"
    merged["Type"] = str(merged["Type"]).upper()
    if merged["Type"] not in SERVER_TYPES:
        merged["Type"] = "PAPER"
    merged["Version"] = str(merged["Version"] or "LATEST").strip()
    merged["JavaVersion"] = str(merged.get("JavaVersion") or "AUTO").upper().strip()
    if merged["JavaVersion"] not in JAVA_VERSIONS:
        merged["JavaVersion"] = "AUTO"
    memory = str(merged["Memory"] or "4G").upper().strip()
    if not re.fullmatch(r"[1-9]\d*[MG]", memory):
        memory = "4G"
    merged["Memory"] = memory
    merged["MaxPlayers"] = min(200, max(1, int(merged["MaxPlayers"])))
    merged["ViewDistance"] = min(32, max(2, int(merged["ViewDistance"])))
    merged["SimulationDistance"] = min(32, max(2, int(merged["SimulationDistance"])))
    merged["SpawnProtection"] = min(128, max(0, int(merged["SpawnProtection"])))
    merged["ModpackUrl"] = "" if legacy_curseforge_selection else str(merged.get("ModpackUrl") or "").strip()
    for key in tuple(merged):
        if key == "ModpackSource" or key.startswith("CurseForge"):
            merged.pop(key, None)
    merged.pop("ModrinthProjects", None)
    merged.pop("ExtraEnv", None)
    return merged


def read_config() -> dict:
    ensure_dirs()
    return normalize_config(read_json(CONFIG_FILE, {}))


def write_config(config: dict) -> None:
    ensure_dirs()
    write_json(CONFIG_FILE, normalize_config(config))


def default_discord_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "webhook_url": "",
        "username": "TechTim Minecraft Server",
        "notify_server_start": True,
        "notify_server_stop": True,
        "notify_server_restart": True,
        "notify_backup": True,
        "notify_errors": True,
    }


def normalize_discord_config(raw: Any) -> dict[str, Any]:
    stored = raw if isinstance(raw, dict) else {}
    defaults = default_discord_config()
    webhook_url = str(stored.get("webhook_url") or "").strip()
    if webhook_url:
        try:
            webhook_url = normalize_webhook_url(webhook_url)
        except ValueError:
            webhook_url = ""
    username = str(stored.get("username") or defaults["username"]).strip()[:80]
    return {
        "enabled": bool(stored.get("enabled", defaults["enabled"])),
        "webhook_url": webhook_url,
        "username": username or defaults["username"],
        "notify_server_start": bool(stored.get("notify_server_start", True)),
        "notify_server_stop": bool(stored.get("notify_server_stop", True)),
        "notify_server_restart": bool(stored.get("notify_server_restart", True)),
        "notify_backup": bool(stored.get("notify_backup", True)),
        "notify_errors": bool(stored.get("notify_errors", True)),
    }


def read_discord_config() -> dict[str, Any]:
    ensure_dirs()
    with DISCORD_CONFIG_LOCK:
        return normalize_discord_config(read_json(DISCORD_CONFIG_FILE, {}))


def write_discord_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_discord_config(config)
    ensure_dirs()
    with DISCORD_CONFIG_LOCK:
        write_json(DISCORD_CONFIG_FILE, normalized)
        try:
            os.chmod(DISCORD_CONFIG_FILE, 0o600)
        except OSError:
            pass
    return normalized


def discord_config_response(config: dict[str, Any] | None = None) -> dict[str, Any]:
    current = config or read_discord_config()
    return {
        "enabled": current["enabled"],
        "username": current["username"],
        "webhook_configured": bool(current["webhook_url"]),
        "webhook_hint": masked_webhook_url(current["webhook_url"]),
        "notify_server_start": current["notify_server_start"],
        "notify_server_stop": current["notify_server_stop"],
        "notify_server_restart": current["notify_server_restart"],
        "notify_backup": current["notify_backup"],
        "notify_errors": current["notify_errors"],
    }


def player_entries(filename: str) -> list[dict[str, Any]]:
    entries = read_json(SERVER_DIR / filename, [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def player_names(filename: str) -> list[str]:
    names = {str(entry.get("name") or "").strip() for entry in player_entries(filename)}
    return sorted((name for name in names if name), key=str.lower)


def parse_online_players(output: str) -> list[str]:
    _, separator, players = clean_log(output).partition(":")
    if not separator:
        return []
    return [name.strip() for name in players.split(",") if name.strip()]


def update_config_player_list(key: str, player: str, add: bool) -> None:
    config = read_config()
    names = [name.strip() for name in re.split(r"[,\n]", str(config.get(key) or "")) if name.strip()]
    matching = {name.lower(): name for name in names}
    if add:
        matching[player.lower()] = player
    else:
        matching.pop(player.lower(), None)
    config[key] = ",".join(sorted(matching.values(), key=str.lower))
    write_config(config)


def docker_client():
    return docker.from_env()


def get_container():
    try:
        return docker_client().containers.get(SERVER_CONTAINER)
    except docker.errors.NotFound:
        return None
    except docker.errors.DockerException:
        return None


def server_running() -> bool:
    try:
        container = get_container()
        if not container:
            return False
        container.reload()
        return container.status == "running"
    except Exception:
        return False


def server_container_accepts_native_console(container) -> bool:
    config = (getattr(container, "attrs", {}) or {}).get("Config") or {}
    return bool(config.get("OpenStdin") and config.get("Tty"))


def send_native_console_command(container, command: str) -> None:
    if not server_container_accepts_native_console(container):
        raise RuntimeError("게임 컨테이너의 Native 콘솔 입력이 활성화되어 있지 않습니다.")

    payload = f"{command}\n".encode("utf-8")
    with SERVER_STDIN_LOCK:
        attached = container.attach_socket(params={
            "stdin": True,
            "stdout": False,
            "stderr": False,
            "stream": True,
        })
        try:
            transport = getattr(attached, "_sock", attached)
            if hasattr(transport, "sendall"):
                transport.sendall(payload)
            elif hasattr(attached, "write"):
                written = attached.write(payload)
                if written is not None and written != len(payload):
                    raise RuntimeError("Native 콘솔 명령을 모두 전송하지 못했습니다.")
                if hasattr(attached, "flush"):
                    attached.flush()
            else:
                raise RuntimeError("Docker Native 콘솔 소켓에 쓸 수 없습니다.")
        finally:
            attached.close()


def default_backup_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "interval_hours": 6,
        "retention_count": 7,
        "last_backup_at": "",
        "next_run_at": "",
    }


def read_backup_config() -> dict[str, Any]:
    raw = read_json(BACKUP_CONFIG_FILE, {})
    defaults = default_backup_config()
    try:
        interval_hours = min(168, max(1, int(raw.get("interval_hours", defaults["interval_hours"]))))
        retention_count = min(50, max(1, int(raw.get("retention_count", defaults["retention_count"]))))
    except (TypeError, ValueError):
        interval_hours = defaults["interval_hours"]
        retention_count = defaults["retention_count"]
    return {
        "enabled": bool(raw.get("enabled", defaults["enabled"])),
        "interval_hours": interval_hours,
        "retention_count": retention_count,
        "last_backup_at": str(raw.get("last_backup_at") or ""),
        "next_run_at": str(raw.get("next_run_at") or ""),
    }


def write_backup_config(config: dict[str, Any]) -> None:
    ensure_dirs()
    write_json(BACKUP_CONFIG_FILE, config)


def write_backup_status(status: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {
        "status": status,
        "message": message,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        **details,
    }
    write_json(BACKUP_STATUS_FILE, payload)
    return payload


def read_backup_status() -> dict[str, Any]:
    return read_json(BACKUP_STATUS_FILE, {
        "status": "idle",
        "message": "백업 기능을 사용할 수 있습니다.",
        "updated_at": "",
    })


def parse_backup_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
    except ValueError:
        return None


def backup_archive_path(filename: str) -> Path:
    if not re.fullmatch(r"minecraft-\d{8}-\d{6}-KST(?:-\d+)?\.tgz", str(filename or "")):
        raise HTTPException(status_code=400, detail="올바르지 않은 백업 파일명입니다.")
    path = (BACKUP_DIR / filename).resolve()
    if path.parent != BACKUP_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="백업 파일을 찾을 수 없습니다.")
    return path


def list_backup_archives() -> list[dict[str, Any]]:
    ensure_dirs()
    entries = []
    for path in BACKUP_DIR.glob("minecraft-*-KST*.tgz"):
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append({
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, KST).isoformat(timespec="seconds"),
        })
    return sorted(entries, key=lambda entry: entry["created_at"], reverse=True)


def run_rcon_command(container, command: str) -> str:
    result = container.exec_run(["rcon-cli", command], stdout=True, stderr=True)
    output = result.output.decode("utf-8", errors="replace") if isinstance(result.output, bytes) else str(result.output or "")
    output = clean_log(output).strip()
    if result.exit_code != 0:
        raise RuntimeError(output or f"RCON 명령 실패: {command}")
    return output


def send_backup_console_command(container, command: str) -> str:
    if server_container_accepts_native_console(container):
        try:
            send_native_console_command(container, command)
        except (docker.errors.DockerException, OSError, RuntimeError) as error:
            raise RuntimeError(f"백업 준비 명령({command})의 Native 콘솔 전송에 실패했습니다: {error}") from error
        return "native"
    try:
        run_rcon_command(container, command)
    except (docker.errors.DockerException, OSError, RuntimeError) as error:
        raise RuntimeError(f"백업 준비 명령({command})을 서버에 전송하지 못했습니다: {error}") from error
    return "rcon"


def minecraft_backup_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    excluded_directories = {"logs", "cache", ".cache", ".tmp", "packs"}
    if any(part in excluded_directories for part in parts) or info.name.endswith(".tmp"):
        return None
    return info


def create_backup_archive(destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            archive.add(SERVER_DIR, arcname=".", recursive=True, filter=minecraft_backup_filter)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def prune_backup_archives(retention_count: int) -> None:
    archives = [BACKUP_DIR / entry["name"] for entry in list_backup_archives()]
    for path in archives[retention_count:]:
        path.unlink(missing_ok=True)


def claim_backup_operation() -> bool:
    global BACKUP_ACTIVE
    with BACKUP_LOCK:
        if BACKUP_ACTIVE or RESTART_OPERATION_ACTIVE:
            return False
        BACKUP_ACTIVE = True
        return True


def backup_operation_active() -> bool:
    with BACKUP_LOCK:
        return BACKUP_ACTIVE


def release_backup_operation() -> None:
    global BACKUP_ACTIVE
    with BACKUP_LOCK:
        BACKUP_ACTIVE = False


def backup_job(reason: str) -> None:
    container = get_container()
    writes_paused = False
    console_transport = "offline"
    filename_base = datetime.now(KST).strftime("minecraft-%Y%m%d-%H%M%S-KST")
    filename = f"{filename_base}.tgz"
    duplicate = 1
    while (BACKUP_DIR / filename).exists():
        filename = f"{filename_base}-{duplicate}.tgz"
        duplicate += 1
    destination = BACKUP_DIR / filename
    write_backup_status("running", "Minecraft 서버 데이터를 백업하고 있습니다.", filename=filename, reason=reason)
    try:
        ensure_dirs()
        if not any(SERVER_DIR.iterdir()):
            raise RuntimeError("백업할 Minecraft 서버 데이터가 없습니다.")
        if container:
            container.reload()
            if container.status == "running":
                console_transport = send_backup_console_command(container, "save-off")
                writes_paused = True
                send_backup_console_command(container, "save-all flush")
                time.sleep(2)
        if hasattr(os, "sync"):
            os.sync()
        create_backup_archive(destination)
        config = read_backup_config()
        prune_backup_archives(config["retention_count"])
        completed_at = datetime.now(KST)
        config["last_backup_at"] = completed_at.isoformat(timespec="seconds")
        config["next_run_at"] = (
            completed_at + timedelta(hours=config["interval_hours"])
        ).isoformat(timespec="seconds") if config["enabled"] else ""
        write_backup_config(config)
        write_backup_status(
            "completed",
            "백업이 완료되었습니다.",
            filename=filename,
            size=destination.stat().st_size,
            reason=reason,
            console_transport=console_transport,
        )
        append_log(CONTROL_LOG_FILE, f"Minecraft 서버 백업 완료: {filename} ({console_transport} 콘솔)")
        notify_discord_event(
            "backup",
            "Minecraft 백업 완료",
            "서버 데이터 백업이 정상적으로 완료되었습니다.",
            [{"name": "백업 파일", "value": filename, "inline": False}],
        )
    except Exception as error:
        destination.unlink(missing_ok=True)
        config = read_backup_config()
        if config["enabled"]:
            config["next_run_at"] = (
                datetime.now(KST) + timedelta(hours=config["interval_hours"])
            ).isoformat(timespec="seconds")
            write_backup_config(config)
        write_backup_status("failed", f"백업에 실패했습니다: {clean_log(str(error))}", filename=filename, reason=reason)
        notify_discord_event("error", "Minecraft 백업 실패", clean_log(str(error)))
    finally:
        if writes_paused and container:
            try:
                container.reload()
                if container.status == "running":
                    send_backup_console_command(container, "save-on")
            except Exception as error:
                append_log(CONTROL_LOG_FILE, f"백업 후 자동 저장 재개 확인 필요: {clean_log(str(error))}")
        release_backup_operation()


def start_backup(reason: str) -> bool:
    if not claim_backup_operation():
        return False
    threading.Thread(target=backup_job, args=(reason,), daemon=True).start()
    return True


def restore_backup_job(source: Path) -> None:
    staging = DATA_DIR / f".minecraft-restore-{secrets.token_hex(6)}"
    previous = DATA_DIR / f".minecraft-before-restore-{secrets.token_hex(6)}"
    write_backup_status("restoring", "선택한 백업을 복원하고 있습니다.", filename=source.name)
    try:
        staging.mkdir(parents=True, exist_ok=False)
        with tarfile.open(source, "r:gz") as archive:
            archive.extractall(staging, filter="data")
        if not any(staging.iterdir()):
            raise RuntimeError("백업 파일에 복원할 서버 데이터가 없습니다.")

        if SERVER_DIR.exists():
            SERVER_DIR.rename(previous)
        try:
            staging.rename(SERVER_DIR)
        except Exception:
            if previous.exists() and not SERVER_DIR.exists():
                previous.rename(SERVER_DIR)
            raise
        write_backup_status("restored", "백업 복원이 완료되었습니다. 서버를 시작해주세요.", filename=source.name)
        append_log(CONTROL_LOG_FILE, f"Minecraft 서버 백업 복원 완료: {source.name}")
        notify_discord_event(
            "backup",
            "Minecraft 백업 복원 완료",
            "선택한 서버 데이터 백업을 정상적으로 복원했습니다.",
            [{"name": "백업 파일", "value": source.name, "inline": False}],
        )
        if previous.exists():
            try:
                shutil.rmtree(previous)
            except OSError as error:
                append_log(
                    CONTROL_LOG_FILE,
                    f"백업 복원 전 데이터 정리 확인 필요: {clean_log(str(error))}",
                )
    except Exception as error:
        write_backup_status("failed", f"백업 복원에 실패했습니다: {clean_log(str(error))}", filename=source.name)
        notify_discord_event("error", "Minecraft 백업 복원 실패", clean_log(str(error)))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if previous.exists() and not SERVER_DIR.exists():
            previous.rename(SERVER_DIR)
        release_backup_operation()


def start_restore(source: Path) -> bool:
    if not claim_backup_operation():
        return False
    threading.Thread(target=restore_backup_job, args=(source,), daemon=True).start()
    return True


def backup_scheduler() -> None:
    while True:
        time.sleep(30)
        try:
            config = read_backup_config()
            if not config["enabled"]:
                continue
            next_run = parse_backup_time(config["next_run_at"])
            if next_run is None:
                next_run = datetime.now(KST) + timedelta(hours=config["interval_hours"])
                config["next_run_at"] = next_run.isoformat(timespec="seconds")
                write_backup_config(config)
                continue
            if datetime.now(KST) >= next_run:
                start_backup("automatic")
        except Exception as error:
            write_backup_status("failed", f"자동 백업 스케줄 확인 실패: {clean_log(str(error))}")


def default_restart_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "restart_time": "04:00",
        "last_run_date": "",
        "last_run_at": "",
        "last_result": "not_run",
        "last_message": "예약 재시작 실행 기록이 없습니다.",
    }


def normalize_restart_schedule(raw: Any) -> dict[str, Any]:
    defaults = default_restart_schedule()
    stored = raw if isinstance(raw, dict) else {}
    restart_time = str(stored.get("restart_time") or defaults["restart_time"]).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", restart_time):
        restart_time = defaults["restart_time"]
    return {
        "enabled": bool(stored.get("enabled", defaults["enabled"])),
        "restart_time": restart_time,
        "last_run_date": str(stored.get("last_run_date") or ""),
        "last_run_at": str(stored.get("last_run_at") or ""),
        "last_result": str(stored.get("last_result") or defaults["last_result"]),
        "last_message": str(stored.get("last_message") or defaults["last_message"]),
    }


def read_restart_schedule() -> dict[str, Any]:
    with RESTART_SCHEDULE_LOCK:
        return normalize_restart_schedule(read_json(RESTART_SCHEDULE_FILE, {}))


def write_restart_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_restart_schedule(schedule)
    ensure_dirs()
    with RESTART_SCHEDULE_LOCK:
        write_json(RESTART_SCHEDULE_FILE, normalized)
    return normalized


def restart_schedule_response(schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    current = schedule or read_restart_schedule()
    next_run_at = ""
    if current["enabled"]:
        hour, minute = (int(part) for part in current["restart_time"].split(":"))
        now = datetime.now(KST)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now or current.get("last_run_date") == now.date().isoformat():
            target += timedelta(days=1)
        next_run_at = target.isoformat(timespec="minutes")
    return {
        **current,
        "timezone": "Asia/Seoul",
        "next_run_at": next_run_at,
        "active": restart_operation_active(),
    }


def restart_operation_active() -> bool:
    with RESTART_OPERATION_LOCK:
        return RESTART_OPERATION_ACTIVE


def claim_restart_operation() -> bool:
    global RESTART_OPERATION_ACTIVE
    with RESTART_OPERATION_LOCK:
        if RESTART_OPERATION_ACTIVE or BACKUP_ACTIVE:
            return False
        RESTART_OPERATION_ACTIVE = True
        return True


def release_restart_operation() -> None:
    global RESTART_OPERATION_ACTIVE
    with RESTART_OPERATION_LOCK:
        RESTART_OPERATION_ACTIVE = False


def update_restart_schedule_result(result: str, message: str) -> None:
    with RESTART_SCHEDULE_LOCK:
        schedule = normalize_restart_schedule(read_json(RESTART_SCHEDULE_FILE, {}))
        schedule["last_result"] = result
        schedule["last_message"] = message
        write_json(RESTART_SCHEDULE_FILE, schedule)


def claim_due_restart(now: datetime) -> dict[str, Any] | None:
    with RESTART_SCHEDULE_LOCK:
        schedule = normalize_restart_schedule(read_json(RESTART_SCHEDULE_FILE, {}))
        today = now.date().isoformat()
        if (
            not schedule["enabled"]
            or now.strftime("%H:%M") != schedule["restart_time"]
            or schedule["last_run_date"] == today
        ):
            return None
        schedule["last_run_date"] = today
        schedule["last_run_at"] = now.isoformat(timespec="seconds")
        schedule["last_result"] = "running"
        schedule["last_message"] = "예약된 Minecraft 서버 재시작을 처리하고 있습니다."
        write_json(RESTART_SCHEDULE_FILE, schedule)
        return schedule


def run_scheduled_restart_if_due() -> None:
    schedule = claim_due_restart(datetime.now(KST))
    if not schedule:
        return
    if not claim_restart_operation():
        message = "백업 또는 복원 작업이 진행 중이어서 이번 예약 재시작을 건너뛰었습니다."
        append_log(CONTROL_LOG_FILE, message)
        update_restart_schedule_result("skipped", message)
        return

    try:
        container = get_container()
        if not container:
            message = "예약 시각에 게임 서버 컨테이너가 없어 재시작을 건너뛰었습니다."
            append_log(CONTROL_LOG_FILE, message)
            update_restart_schedule_result("skipped", message)
            return
        container.reload()
        if container.status != "running":
            message = "예약 시각에 Minecraft 서버가 실행 중이 아니어서 재시작을 건너뛰었습니다."
            append_log(CONTROL_LOG_FILE, message)
            update_restart_schedule_result("skipped", message)
            return

        append_log(
            CONTROL_LOG_FILE,
            f"매일 {schedule['restart_time']} 한국표준시 예약에 따라 Minecraft 게임 컨테이너를 재시작합니다.",
        )
        try:
            send_backup_console_command(container, "save-all flush")
            time.sleep(2)
        except (docker.errors.DockerException, OSError, RuntimeError) as error:
            append_log(CONTROL_LOG_FILE, f"예약 재시작 전 월드 저장 확인 필요: {clean_log(str(error))}")
        suppress_discord_runtime_alerts()
        container.restart(timeout=60)
        message = "예약된 Minecraft 게임 컨테이너 재시작이 완료되었습니다."
        append_log(CONTROL_LOG_FILE, message)
        update_restart_schedule_result("success", message)
        notify_discord_event(
            "server_restart",
            "Minecraft 예약 재시작 완료",
            f"매일 {schedule['restart_time']} 한국표준시 예약에 따라 게임 컨테이너를 재시작했습니다.",
        )
    except Exception as error:
        message = f"예약 재시작 중 오류가 발생했습니다: {clean_log(str(error))}"
        append_log(CONTROL_LOG_FILE, message)
        update_restart_schedule_result("error", message)
        notify_discord_event("error", "Minecraft 예약 재시작 실패", clean_log(str(error)))
    finally:
        release_restart_operation()


def restart_scheduler_loop() -> None:
    while not RESTART_SCHEDULER_STOP.wait(5):
        try:
            run_scheduled_restart_if_due()
        except Exception as error:
            append_log(
                CONTROL_LOG_FILE,
                f"예약 재시작 스케줄 확인 중 오류가 발생했습니다: {clean_log(str(error))}",
            )


def ensure_restart_scheduler_running() -> None:
    global RESTART_SCHEDULER_THREAD
    with RESTART_SCHEDULER_THREAD_LOCK:
        if RESTART_SCHEDULER_THREAD and RESTART_SCHEDULER_THREAD.is_alive():
            return
        RESTART_SCHEDULER_STOP.clear()
        RESTART_SCHEDULER_THREAD = threading.Thread(
            target=restart_scheduler_loop,
            daemon=True,
            name="minecraft-restart-scheduler",
        )
        RESTART_SCHEDULER_THREAD.start()


def public_server_ip() -> str:
    configured = str(os.getenv("PUBLIC_IP") or "").strip()
    if configured:
        return configured

    now = time.monotonic()
    with PUBLIC_IP_LOCK:
        if now < float(PUBLIC_IP_CACHE.get("expires_at") or 0):
            return str(PUBLIC_IP_CACHE.get("value") or "")

        value = ""
        try:
            metadata_request = UrlRequest(
                "http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip",
                headers={"Metadata-Flavor": "Google"},
            )
            with urlopen(metadata_request, timeout=1.5) as response:
                value = response.read(64).decode("ascii", errors="ignore").strip()
            if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value):
                value = ""
        except Exception:
            value = ""

        PUBLIC_IP_CACHE["value"] = value
        PUBLIC_IP_CACHE["expires_at"] = now + (300 if value else 60)
        return value


def discord_event_fields(extra_fields: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    config = read_config()
    address = public_server_ip()
    fields = [
        {"name": "서버", "value": config.get("ServerName") or "Minecraft Server", "inline": True},
        {"name": "발생 시각", "value": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"), "inline": True},
    ]
    if address:
        fields.append({"name": "접속 주소", "value": address, "inline": True})
    fields.extend(extra_fields or [])
    return fields


def deliver_discord_event(
    config: dict[str, Any],
    *,
    event: str,
    title: str,
    message: str,
    fields: list[dict[str, Any]] | None = None,
) -> None:
    payload = build_webhook_payload(
        username=config["username"],
        title=title,
        description=message,
        color=DISCORD_EVENT_COLORS.get(event, DISCORD_EVENT_COLORS["test"]),
        fields=discord_event_fields(fields),
    )
    execute_webhook(config["webhook_url"], payload)


def notify_discord_event(
    event: str,
    title: str,
    message: str,
    fields: list[dict[str, Any]] | None = None,
) -> None:
    config = read_discord_config()
    setting = DISCORD_EVENT_SETTINGS.get(event)
    if not config["enabled"] or not config["webhook_url"] or (setting and not config.get(setting, False)):
        return

    def send() -> None:
        try:
            deliver_discord_event(config, event=event, title=title, message=message, fields=fields)
        except Exception as error:
            append_log(CONTROL_LOG_FILE, f"Discord 알림 전송 실패: {clean_log(str(error))}")

    threading.Thread(target=send, daemon=True, name=f"discord-notify-{event}").start()


def suppress_discord_runtime_alerts(seconds: int = 120) -> None:
    global DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL
    with DISCORD_RUNTIME_STATE_LOCK:
        DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL = max(
            DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL,
            time.monotonic() + seconds,
        )


def claim_discord_runtime_alert() -> bool:
    global DISCORD_RUNTIME_LAST_ALERT_AT
    now = time.monotonic()
    with DISCORD_RUNTIME_STATE_LOCK:
        if now < DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL or now - DISCORD_RUNTIME_LAST_ALERT_AT < 300:
            return False
        DISCORD_RUNTIME_LAST_ALERT_AT = now
        return True


def current_discord_runtime_state() -> dict[str, Any]:
    container = get_container()
    if not container:
        return {"container_id": "", "status": "not_created", "restart_count": 0, "exit_code": 0}
    try:
        container.reload()
        attrs = container.attrs or {}
        state = attrs.get("State") or {}
        return {
            "container_id": container.id,
            "status": str(container.status or "unknown"),
            "restart_count": int(attrs.get("RestartCount") or 0),
            "exit_code": int(state.get("ExitCode") or 0),
        }
    except (docker.errors.DockerException, TypeError, ValueError):
        return {"container_id": "", "status": "unknown", "restart_count": 0, "exit_code": 0}


def discord_runtime_monitor_loop() -> None:
    while not DISCORD_MONITOR_STOP.wait(5):
        current = current_discord_runtime_state()
        with DISCORD_RUNTIME_STATE_LOCK:
            previous = dict(DISCORD_RUNTIME_STATE)
            DISCORD_RUNTIME_STATE.clear()
            DISCORD_RUNTIME_STATE.update(current)
            suppressed = time.monotonic() < DISCORD_RUNTIME_ALERTS_SUPPRESSED_UNTIL

        if suppressed or not previous or previous.get("container_id") != current.get("container_id"):
            continue
        restart_count = int(current.get("restart_count") or 0)
        previous_restart_count = int(previous.get("restart_count") or 0)
        if restart_count > previous_restart_count and claim_discord_runtime_alert():
            notify_discord_event(
                "error",
                "Minecraft 서버 자동 재기동 감지",
                "게임 컨테이너가 비정상 종료된 후 Docker 정책에 따라 자동으로 다시 시작되었습니다.",
                [{"name": "재시작 횟수", "value": str(restart_count), "inline": True}],
            )
            continue
        if (
            previous.get("status") == "running"
            and current.get("status") in {"exited", "dead"}
            and int(current.get("exit_code") or 0) != 0
            and claim_discord_runtime_alert()
        ):
            notify_discord_event(
                "error",
                "Minecraft 서버 비정상 종료",
                "게임 컨테이너가 예상하지 못한 상태로 종료되었습니다.",
                [{"name": "Exit Code", "value": str(current.get("exit_code")), "inline": True}],
            )


def ensure_discord_monitor_running() -> None:
    global DISCORD_MONITOR_THREAD
    with DISCORD_MONITOR_THREAD_LOCK:
        if DISCORD_MONITOR_THREAD and DISCORD_MONITOR_THREAD.is_alive():
            return
        DISCORD_MONITOR_STOP.clear()
        with DISCORD_RUNTIME_STATE_LOCK:
            DISCORD_RUNTIME_STATE.clear()
            DISCORD_RUNTIME_STATE.update(current_discord_runtime_state())
        DISCORD_MONITOR_THREAD = threading.Thread(
            target=discord_runtime_monitor_loop,
            daemon=True,
            name="minecraft-discord-monitor",
        )
        DISCORD_MONITOR_THREAD.start()


def parse_os_cpu_stat(value: str) -> dict[int, tuple[int, int]]:
    samples: dict[int, tuple[int, int]] = {}
    for line in value.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"cpu\d+", parts[0]):
            continue
        try:
            counters = [int(counter) for counter in parts[1:9]]
        except ValueError:
            continue
        if len(counters) < 4:
            continue
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        samples[int(parts[0][3:])] = (sum(counters), idle)
    return samples


def os_cpu_usage() -> tuple[float, list[dict[str, Any]]]:
    try:
        current = parse_os_cpu_stat(PROC_STAT_FILE.read_text(encoding="ascii", errors="ignore"))
    except OSError:
        current = {}
    with OS_CPU_LOCK:
        previous = dict(OS_CPU_SAMPLE)
        OS_CPU_SAMPLE.clear()
        OS_CPU_SAMPLE.update(current)

    threads = []
    for index in sorted(current):
        total, idle = current[index]
        previous_total, previous_idle = previous.get(index, (total, idle))
        total_delta = total - previous_total
        idle_delta = idle - previous_idle
        percent = (total_delta - idle_delta) / total_delta * 100 if total_delta > 0 else 0.0
        threads.append({"thread": index + 1, "percent": round(min(100.0, max(0.0, percent)), 1)})
    average = sum(thread["percent"] for thread in threads) / len(threads) if threads else 0.0
    return round(average, 1), threads


def os_memory_usage() -> dict[str, int | float]:
    values: dict[str, int] = {}
    try:
        for line in PROC_MEMINFO_FILE.read_text(encoding="ascii", errors="ignore").splitlines():
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            match = re.search(r"\d+", raw)
            if match:
                values[key] = int(match.group()) * 1024
    except OSError:
        values = {}

    total = max(0, values.get("MemTotal", 0))
    available = max(0, values.get("MemAvailable", 0))
    if total and not available:
        available = max(0, sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached", "SReclaimable")) - values.get("Shmem", 0))
    if not total:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total = page_size * int(os.sysconf("SC_PHYS_PAGES"))
            available = page_size * int(os.sysconf("SC_AVPHYS_PAGES"))
        except (OSError, ValueError):
            total = 0
            available = 0

    available = min(total, available) if total else 0
    used = max(0, total - available)
    swap_total = max(0, values.get("SwapTotal", 0))
    swap_free = min(swap_total, max(0, values.get("SwapFree", 0))) if swap_total else 0
    swap_used = max(0, swap_total - swap_free)
    percent = used / total * 100 if total else 0.0
    return {
        "memory_percent": round(min(100.0, max(0.0, percent)), 1),
        "memory_used": used,
        "memory_total": total,
        "memory_available": available,
        "swap_used": swap_used,
        "swap_total": swap_total,
    }


def container_resource_usage(container) -> dict[str, Any]:
    stats = container.stats(stream=False)
    memory = stats.get("memory_stats") or {}
    memory_stats = memory.get("stats") or {}
    memory_cache = int(memory_stats.get("total_inactive_file") or memory_stats.get("inactive_file") or 0)
    memory_used = max(0, int(memory.get("usage") or 0) - memory_cache)
    memory_limit = max(0, int(memory.get("limit") or 0))
    memory_percent = memory_used / memory_limit * 100 if memory_limit else 0.0

    networks = stats.get("networks") or {}
    received = sum(max(0, int(item.get("rx_bytes") or 0)) for item in networks.values())
    sent = sum(max(0, int(item.get("tx_bytes") or 0)) for item in networks.values())
    sampled_at = time.monotonic()
    container_id = str(getattr(container, "id", "") or "")
    with RESOURCE_LOCK:
        previous_id = str(RESOURCE_NETWORK_SAMPLE.get("container_id") or "")
        elapsed = sampled_at - float(RESOURCE_NETWORK_SAMPLE.get("sampled_at") or 0)
        if previous_id == container_id and elapsed > 0:
            received_per_second = max(0.0, (received - int(RESOURCE_NETWORK_SAMPLE.get("received") or 0)) / elapsed)
            sent_per_second = max(0.0, (sent - int(RESOURCE_NETWORK_SAMPLE.get("sent") or 0)) / elapsed)
        else:
            received_per_second = 0.0
            sent_per_second = 0.0
        RESOURCE_NETWORK_SAMPLE.update({
            "container_id": container_id,
            "sampled_at": sampled_at,
            "received": received,
            "sent": sent,
        })

    return {
        "game_memory_percent": round(min(100.0, max(0.0, memory_percent)), 1),
        "game_memory_used": memory_used,
        "game_memory_limit": memory_limit,
        "network_received_per_second": round(received_per_second),
        "network_sent_per_second": round(sent_per_second),
    }


def empty_game_metrics(config: dict, status: str = "stopped", message: str = "게임 서버가 중지되어 있습니다.") -> dict[str, Any]:
    return {
        "status": status,
        "supported": False,
        "source": "",
        "health": "unknown",
        "tps": None,
        "tps_1m": None,
        "tps_5m": None,
        "tps_15m": None,
        "mspt": None,
        "mspt_min": None,
        "mspt_p95": None,
        "mspt_max": None,
        "players_online": 0,
        "players_max": int(config.get("MaxPlayers") or 0),
        "players_available": False,
        "uptime_seconds": 0,
        "message": message,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
    }


def try_rcon_metric_command(container, command: str) -> str:
    try:
        return run_rcon_command(container, command)
    except (docker.errors.DockerException, OSError, RuntimeError):
        return ""


def collect_game_metrics(container, config: dict) -> dict[str, Any]:
    metrics = empty_game_metrics(config, status="starting", message="Minecraft 명령 인터페이스를 확인하고 있습니다.")
    state = (getattr(container, "attrs", {}) or {}).get("State") or {}
    metrics["uptime_seconds"] = container_uptime_seconds(str(state.get("StartedAt") or ""))

    list_output = try_rcon_metric_command(container, "list")
    if list_output:
        online, maximum = parse_player_counts(list_output, metrics["players_max"])
        metrics.update({
            "players_online": online,
            "players_max": maximum,
            "players_available": True,
        })

    server_type = str(config.get("Type") or "VANILLA").upper()
    version = str(config.get("Version") or "LATEST")
    source = ""
    performance: dict[str, float] = {}

    if server_type in {"PAPER", "PURPUR", "SPIGOT"}:
        tps_output = try_rcon_metric_command(container, "tps")
        mspt_output = try_rcon_metric_command(container, "mspt")
        performance.update(parse_paper_tps(tps_output))
        performance.update(parse_paper_mspt(mspt_output))
        if performance:
            source = "Paper/Spigot"
    elif server_type == "FORGE":
        performance.update(parse_forge_tps(try_rcon_metric_command(container, "forge tps")))
        if performance:
            source = "Forge"
    elif server_type == "NEOFORGE":
        performance.update(parse_forge_tps(try_rcon_metric_command(container, "forge tps")))
        if not performance:
            performance.update(parse_forge_tps(try_rcon_metric_command(container, "neoforge tps")))
        if performance:
            source = "NeoForge"

    if not performance and server_type in {"FABRIC", "QUILT", "NEOFORGE"}:
        performance.update(parse_spark_tps(try_rcon_metric_command(container, "spark tps")))
        if performance:
            source = "Spark"

    if (performance.get("tps") is None or performance.get("mspt") is None) and version_at_least(version, (1, 20, 3)):
        tick_metrics = parse_tick_query(try_rcon_metric_command(container, "tick query"))
        for key, value in tick_metrics.items():
            performance.setdefault(key, value)
        if tick_metrics and not source:
            source = "Minecraft tick"

    metrics.update(performance)
    metrics["supported"] = metrics["tps"] is not None or metrics["mspt"] is not None
    metrics["source"] = source
    metrics["health"] = health_state(metrics)
    metrics["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    if metrics["supported"]:
        metrics["status"] = "ready"
        metrics["message"] = "Minecraft 성능 지표를 정상적으로 수집하고 있습니다."
    elif list_output:
        metrics["status"] = "unsupported"
        metrics["message"] = "현재 서버 유형 또는 버전은 TPS/MSPT 조회 명령을 제공하지 않습니다."
    else:
        metrics["status"] = "starting"
        metrics["message"] = "서버 시작이 완료되면 Minecraft 성능 지표가 표시됩니다."
    return metrics


def cached_game_metrics(container, config: dict) -> dict[str, Any]:
    container_id = str(getattr(container, "id", "") or "")
    now = time.monotonic()
    with GAME_METRICS_LOCK:
        cached = GAME_METRICS_CACHE.get("payload")
        if (
            cached
            and str(GAME_METRICS_CACHE.get("container_id") or "") == container_id
            and now < float(GAME_METRICS_CACHE.get("expires_at") or 0)
        ):
            return dict(cached)
        payload = collect_game_metrics(container, config)
        GAME_METRICS_CACHE.update({
            "container_id": container_id,
            "expires_at": time.monotonic() + GAME_METRICS_CACHE_SECONDS,
            "payload": payload,
        })
        return dict(payload)


def runtime_image_for_config(config: dict | None = None) -> str:
    java_version = str((config or read_config()).get("JavaVersion") or "AUTO").upper()
    if java_version == "AUTO":
        return RUNTIME_IMAGE
    repository, _ = docker.utils.parse_repository_tag(RUNTIME_IMAGE)
    return f"{repository}:java{java_version}"


def installed(config: dict | None = None) -> bool:
    if not INSTALL_MARKER_FILE.exists():
        return False
    runtime_image = runtime_image_for_config(config)
    return f"runtime_image={runtime_image}" in INSTALL_MARKER_FILE.read_text(encoding="utf-8")


def pull_image_with_progress(client, runtime_image: str) -> None:
    repository, tag = docker.utils.parse_repository_tag(runtime_image)
    tag = tag or "latest"
    events: queue.Queue = queue.Queue()

    def consume() -> None:
        try:
            for event in client.api.pull(repository, tag=tag, stream=True, decode=True):
                events.put(("event", event))
        except Exception as exc:
            events.put(("error", exc))
        finally:
            events.put(("done", None))

    threading.Thread(target=consume, daemon=True).start()
    started = time.monotonic()
    latest_status: dict[str, str] = {}
    while True:
        try:
            kind, payload = events.get(timeout=PULL_HEARTBEAT_SECONDS)
        except queue.Empty:
            elapsed = int(time.monotonic() - started)
            append_log(INSTALL_LOG_FILE, f"[docker] 이미지 처리가 계속 진행 중입니다. 경과 {elapsed // 60}분 {elapsed % 60}초")
            continue
        if kind == "error":
            raise payload
        if kind == "done":
            break
        if not isinstance(payload, dict):
            continue
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        status = clean_log(payload.get("status", "")).strip()
        layer = str(payload.get("id") or "image")
        progress = clean_log(payload.get("progress", "")).strip()
        if status and latest_status.get(layer) != status:
            append_log(INSTALL_LOG_FILE, f"[docker] {layer}: {status}{' ' + progress if progress else ''}")
            latest_status[layer] = status
    client.images.get(runtime_image)


def install_job(runtime_image: str) -> None:
    global INSTALL_ACTIVE
    with INSTALL_LOCK:
        INSTALL_ACTIVE = True
    ensure_dirs()
    INSTALL_LOG_FILE.write_text("", encoding="utf-8")
    INSTALL_STATUS_FILE.write_text("running", encoding="utf-8")
    try:
        append_log(INSTALL_LOG_FILE, "Minecraft Java 서버 설치 작업을 시작합니다.")
        append_log(INSTALL_LOG_FILE, f"공식 itzg Docker 이미지: {runtime_image}")
        pull_image_with_progress(docker_client(), runtime_image)
        INSTALL_MARKER_FILE.write_text(
            f"distribution=itzg-docker\nruntime_image={runtime_image}\ninstalled_at={datetime.now().isoformat()}\n",
            encoding="utf-8",
        )
        INSTALL_STATUS_FILE.write_text("completed", encoding="utf-8")
        append_log(INSTALL_LOG_FILE, "Minecraft 게임 엔진 이미지 설치가 완료되었습니다.")
        append_log(INSTALL_LOG_FILE, "서버 설정을 저장한 뒤 서버 시작을 눌러주세요.")
    except Exception as exc:
        INSTALL_STATUS_FILE.write_text("failed", encoding="utf-8")
        append_log(INSTALL_LOG_FILE, f"설치 실패: {exc}")
    finally:
        with INSTALL_LOCK:
            INSTALL_ACTIVE = False


def default_panel_update_status() -> dict:
    return {
        "status": "idle",
        "message": "TechTim 구동기 업데이트를 확인할 수 있습니다.",
        "progress": 0,
        "current_image_id": "",
        "latest_image_id": "",
        "updated_at": "",
    }


def write_panel_update_status(status: str, message: str, **details) -> dict:
    ensure_dirs()
    payload = default_panel_update_status()
    payload.update(details)
    payload.update({
        "status": status,
        "message": message,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
    })
    temporary_path = PANEL_UPDATE_STATUS_FILE.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(PANEL_UPDATE_STATUS_FILE)
    return payload


def read_panel_update_status() -> dict:
    stored = read_json(PANEL_UPDATE_STATUS_FILE, {})
    if not isinstance(stored, dict):
        return default_panel_update_status()
    status = default_panel_update_status()
    status.update(stored)
    return status


def panel_update_check_payload() -> dict:
    now = time.monotonic()
    with PANEL_UPDATE_CHECK_LOCK:
        cached = PANEL_UPDATE_CHECK_CACHE.get("payload")
        if cached and now < float(PANEL_UPDATE_CHECK_CACHE.get("expires_at") or 0):
            return dict(cached)

        try:
            client = docker_client()
            current_container = client.containers.get(PANEL_CONTAINER_NAME)
            current_container.reload()
            current_image = current_container.image
            current_image.reload()
            current_image_id = current_image.id
            current_digests = {
                str(value).rsplit("@", 1)[-1].lower()
                for value in (current_image.attrs.get("RepoDigests") or [])
                if "@" in str(value)
            }
            latest_image_id = str(client.images.get_registry_data(PANEL_IMAGE).id or "").lower()

            if not latest_image_id or not current_digests:
                raise RuntimeError("실행 중인 패널 이미지의 digest를 확인할 수 없습니다.")

            payload = {
                "status": "ok",
                "update_available": latest_image_id not in current_digests,
                "current_version": PANEL_VERSION,
                "current_image_id": current_image_id,
                "latest_image_id": latest_image_id,
            }
            cache_seconds = 300
        except Exception as exc:
            payload = {
                "status": "unavailable",
                "update_available": False,
                "current_version": PANEL_VERSION,
                "message": clean_log(str(exc)),
            }
            cache_seconds = 60

        PANEL_UPDATE_CHECK_CACHE["payload"] = payload
        PANEL_UPDATE_CHECK_CACHE["expires_at"] = now + cache_seconds
        return dict(payload)


def is_panel_updater_running() -> bool:
    try:
        updater = docker_client().containers.get(f"{PANEL_CONTAINER_NAME}-updater")
        updater.reload()
        return updater.status in {"created", "running", "restarting"}
    except docker.errors.NotFound:
        return False
    except Exception:
        return False


def panel_pull_event_progress(status: str, progress_detail: dict[str, Any]) -> float | None:
    normalized = status.strip().lower()
    current = float(progress_detail.get("current") or 0)
    total = float(progress_detail.get("total") or 0)
    ratio = min(1.0, current / total) if total > 0 else 0.0
    if normalized in {"already exists", "pull complete"}:
        return 1.0
    if normalized == "extracting":
        return 0.7 + ratio * 0.3
    if normalized in {"download complete", "verifying checksum"}:
        return 0.7
    if normalized == "downloading":
        return ratio * 0.7
    if normalized in {"pulling fs layer", "waiting"}:
        return 0.0
    return None


def pull_panel_image_with_progress(client, current_image_id: str):
    repository, tag = docker.utils.parse_repository_tag(PANEL_IMAGE)
    tag = tag or "latest"
    layers: dict[str, float] = {}
    last_progress = 10
    for event in client.api.pull(repository, tag=tag, stream=True, decode=True):
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            raise RuntimeError(str(event["error"]))
        layer = str(event.get("id") or "").strip()
        layer_progress = panel_pull_event_progress(
            clean_log(event.get("status", "")),
            event.get("progressDetail") if isinstance(event.get("progressDetail"), dict) else {},
        )
        if layer and layer_progress is not None:
            layers[layer] = max(layers.get(layer, 0.0), layer_progress)
        if not layers:
            continue
        progress = min(84, 10 + int(74 * sum(layers.values()) / len(layers)))
        if progress <= last_progress:
            continue
        last_progress = progress
        write_panel_update_status(
            "downloading",
            "최신 TechTim 구동기 이미지를 다운로드하고 있습니다.",
            progress=progress,
            current_image_id=current_image_id,
        )

    image = client.images.get(PANEL_IMAGE)
    write_panel_update_status(
        "downloading",
        "최신 이미지 다운로드가 완료되어 무결성을 확인하고 있습니다.",
        progress=85,
        current_image_id=current_image_id,
        latest_image_id=image.id,
    )
    return image


def panel_update_job() -> None:
    global PANEL_UPDATE_ACTIVE

    try:
        write_panel_update_status(
            "checking",
            "현재 TechTim 구동기 이미지와 최신 이미지를 비교하고 있습니다.",
            progress=5,
        )
        client = docker_client()
        current_container = client.containers.get(PANEL_CONTAINER_NAME)
        current_container.reload()
        current_image_id = current_container.image.id

        write_panel_update_status(
            "downloading",
            "최신 TechTim 구동기 이미지를 확인하고 있습니다. 이미지 다운로드에는 시간이 걸릴 수 있습니다.",
            progress=10,
            current_image_id=current_image_id,
        )
        latest_image = pull_panel_image_with_progress(client, current_image_id)
        latest_image_id = latest_image.id

        if current_image_id == latest_image_id:
            write_panel_update_status(
                "not_required",
                "이미 최신 버전의 TechTim 구동기를 사용하고 있어 업데이트가 필요하지 않습니다.",
                progress=100,
                current_image_id=current_image_id,
                latest_image_id=latest_image_id,
            )
            return

        updater_name = f"{PANEL_CONTAINER_NAME}-updater"
        try:
            stale_updater = client.containers.get(updater_name)
            stale_updater.reload()
            if stale_updater.status == "running":
                raise RuntimeError("이미 TechTim 구동기 교체 작업이 실행 중입니다.")
            stale_updater.remove(force=True)
        except docker.errors.NotFound:
            pass

        write_panel_update_status(
            "restarting",
            "최신 이미지 다운로드가 완료되었습니다. 패널 컨테이너를 교체하고 있습니다.",
            progress=90,
            current_image_id=current_image_id,
            latest_image_id=latest_image_id,
        )
        client.containers.run(
            PANEL_IMAGE,
            command=["python", "-m", "app.self_update"],
            name=updater_name,
            detach=True,
            auto_remove=True,
            environment={
                "TARGET_CONTAINER": PANEL_CONTAINER_NAME,
                "PROXY_CONTAINER": PANEL_PROXY_CONTAINER,
                "TARGET_IMAGE": PANEL_IMAGE,
                "PANEL_UPDATE_STATUS_FILE": "/update-data/panel-update-status.json",
                "PANEL_UPDATE_DELAY_SECONDS": "2",
            },
            volumes={
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
                str(HOST_DATA_DIR): {
                    "bind": "/update-data",
                    "mode": "rw",
                },
            },
        )
    except Exception as error:
        failed_progress = int(read_panel_update_status().get("progress") or 0)
        write_panel_update_status(
            "failed",
            f"TechTim 구동기 업데이트에 실패했습니다: {error}",
            progress=failed_progress,
        )
    finally:
        with PANEL_UPDATE_LOCK:
            PANEL_UPDATE_ACTIVE = False


def bool_env(value: Any) -> str:
    return "TRUE" if bool(value) else "FALSE"


def runtime_environment(config: dict) -> dict[str, str]:
    env = {
        "EULA": "TRUE",
        "TYPE": config["Type"],
        "VERSION": config["Version"],
        "MEMORY": config["Memory"],
        "SERVER_NAME": config["ServerName"],
        "MOTD": config["Motd"],
        "LEVEL": config["Level"],
        "DIFFICULTY": config["Difficulty"],
        "MODE": config["GameMode"],
        "MAX_PLAYERS": str(config["MaxPlayers"]),
        "ONLINE_MODE": bool_env(config["OnlineMode"]),
        "PVP": bool_env(config["Pvp"]),
        "ALLOW_FLIGHT": bool_env(config["AllowFlight"]),
        "ENABLE_COMMAND_BLOCK": bool_env(config["EnableCommandBlock"]),
        "VIEW_DISTANCE": str(config["ViewDistance"]),
        "SIMULATION_DISTANCE": str(config["SimulationDistance"]),
        "SPAWN_PROTECTION": str(config["SpawnProtection"]),
        "ENABLE_RCON": "TRUE",
        "TZ": "Asia/Seoul",
    }
    optional = {
        "SEED": config["Seed"], "WHITELIST": config["Whitelist"], "OPS": config["Ops"],
    }
    if config["Type"] in MODPACK_URL_TYPES:
        optional["GENERIC_PACK"] = config["ModpackUrl"]
    if config["Type"] == "FORGE" and config["ModpackUrl"]:
        optional["FORGE_VERSION"] = "latest"
    for key, value in optional.items():
        if str(value or "").strip():
            env[key] = str(value).strip()
    return env


def runtime_config_signature(runtime_image: str, environment: dict[str, str]) -> str:
    payload = {
        "image": runtime_image,
        "environment": sorted((str(key), str(value)) for key, value in environment.items()),
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def container_matches_runtime(container, runtime_image: str, environment: dict[str, str]) -> bool:
    expected_signature = runtime_config_signature(runtime_image, environment)
    attrs = getattr(container, "attrs", {}) or {}
    config = attrs.get("Config") or {}
    if not server_container_accepts_native_console(container):
        return False
    labels = config.get("Labels") or {}
    stored_signature = str(labels.get(RUNTIME_CONFIG_LABEL) or "")
    if stored_signature:
        return secrets.compare_digest(stored_signature, expected_signature)

    # Containers created by an older panel do not have the signature label.
    # Compare their image and panel-managed environment once so they can still be reused.
    if str(config.get("Image") or "") != runtime_image:
        return False
    current_environment = {}
    for entry in config.get("Env") or []:
        key, separator, value = str(entry).partition("=")
        if separator:
            current_environment[key] = value
    return all(current_environment.get(key) == str(value) for key, value in environment.items())


def validate_start(config: dict) -> None:
    if config["ModpackUrl"] and not re.match(r"^https?://\S+$", config["ModpackUrl"], re.IGNORECASE):
        raise HTTPException(status_code=400, detail="모드팩 URL은 http:// 또는 https:// 주소로 입력해주세요.")


def resolve_path(relative: str = "") -> Path:
    root = SERVER_DIR.resolve()
    target = (root / str(relative or "").replace("\\", "/").lstrip("/")).resolve()
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise HTTPException(status_code=400, detail="서버 데이터 폴더 밖으로 이동할 수 없습니다.")
    return target


def resolve_folder_upload_target(target_dir: Path, upload_path: str) -> Path:
    cleaned = str(upload_path or "").strip().replace("\\", "/")
    parts = cleaned.split("/")
    if not cleaned or cleaned.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="폴더 업로드에 허용되지 않는 상대 경로가 포함되어 있습니다.")

    try:
        target_root = target_dir.resolve()
        target = target_root.joinpath(*parts).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="폴더 업로드 경로를 처리할 수 없습니다.")

    if target == target_root or not str(target).startswith(str(target_root) + os.sep):
        raise HTTPException(status_code=400, detail="폴더 업로드 경로가 현재 폴더 밖을 가리킵니다.")

    resolve_path(relative_path(target))
    return target


def relative_path(path: Path) -> str:
    root = SERVER_DIR.resolve()
    return "" if path.resolve() == root else path.resolve().relative_to(root).as_posix()


def resolve_download_target(relative: str) -> Path:
    cleaned = str(relative or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned or any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise HTTPException(status_code=400, detail="다운로드할 항목 경로가 올바르지 않습니다.")
    candidate = SERVER_DIR.joinpath(*cleaned.split("/"))
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="심볼릭 링크는 다운로드할 수 없습니다.")
    target = resolve_path(cleaned)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"다운로드할 항목을 찾을 수 없습니다: {relative}")
    return target


def create_selected_download_archive(targets: list[Path]) -> tuple[Path, str]:
    ensure_dirs()
    root = SERVER_DIR.resolve()
    timestamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    download_name = f"minecraft-files-{timestamp}.zip"
    archive_path = DOWNLOAD_EXPORT_DIR / f".{download_name}.{secrets.token_hex(6)}.tmp"
    selected: list[Path] = []
    selected_directories: list[Path] = []
    for target in sorted(targets, key=lambda item: len(item.relative_to(root).parts)):
        if any(parent == target or parent in target.parents for parent in selected_directories):
            continue
        selected.append(target)
        if target.is_dir():
            selected_directories.append(target)

    written: set[str] = set()
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for target in selected:
                if target.is_file():
                    archive_name = target.relative_to(root).as_posix()
                    if archive_name not in written:
                        archive.write(target, arcname=archive_name)
                        written.add(archive_name)
                    continue

                for current_dir, dirnames, filenames in os.walk(target, followlinks=False):
                    current = Path(current_dir)
                    dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
                    directory_name = current.relative_to(root).as_posix().rstrip("/") + "/"
                    if directory_name not in written:
                        archive.writestr(directory_name, b"")
                        written.add(directory_name)
                    for filename in filenames:
                        source = current / filename
                        if source.is_symlink() or not source.is_file():
                            continue
                        resolved = source.resolve()
                        if not str(resolved).startswith(str(root) + os.sep):
                            continue
                        archive_name = source.relative_to(root).as_posix()
                        if archive_name not in written:
                            archive.write(source, arcname=archive_name)
                            written.add(archive_name)
        return archive_path, download_name
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def login_html(change: bool = False) -> str:
    title = "새 관리자 비밀번호" if change else "관리자 로그인"
    fields = ('<input id="password2" type="password" placeholder="새 비밀번호 확인" required>' if change else '')
    hint = "" if change else """<div class='hint'>
      최초 기본 계정은 <b>admin / admin</b> 입니다.<br>
      첫 로그인 후 반드시 비밀번호를 변경해야 합니다.
    </div>"""
    script = """
      const path='/api/auth/change-password'; const body={new_password:password.value};
      if(password.value!==password2.value){msg.textContent='비밀번호가 일치하지 않습니다.';return;}
    """ if change else "const path='/api/auth/login'; const body={username:username.value,password:password.value};"
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{title}</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#142116 url('/static/minecraft-panel-background.png') center/cover fixed;font-family:Arial,sans-serif;color:#fff}}form{{width:min(390px,calc(100% - 32px));padding:30px;background:rgba(12,25,18,.88);border:1px solid #7daa69;border-radius:8px;box-shadow:0 20px 60px #0009}}h1{{font-size:23px;margin:0 0 8px}}p{{color:#c9d6ca}}input,button{{width:100%;height:46px;margin-top:12px;border-radius:5px;border:1px solid #607666;padding:0 13px;font-size:15px}}button{{background:#5f8f45;color:#fff;font-weight:700;cursor:pointer}}.hint{{margin-top:16px;padding:12px;border:1px solid #607666;border-radius:5px;background:rgba(255,255,255,.08);color:#dfe9df;font-size:13px;line-height:1.55}}.hint b{{color:#fff}}#msg{{color:#ffcf70;min-height:20px}}</style></head>
    <body><form id='form'><h1>{title}</h1><p>{'현재 비밀번호 입력 없이 새 비밀번호만 설정합니다.' if change else 'TechTim Minecraft Server Panel'}</p>{'' if change else "<input id='username' value='admin' autocomplete='username' required>"}<input id='password' type='password' placeholder='비밀번호' autocomplete='current-password' required>{fields}<button>확인</button>{hint}<p id='msg'></p></form>
    <script>form.addEventListener('submit',async e=>{{e.preventDefault();{script}const r=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});const d=await r.json();if(!r.ok){{msg.textContent=d.detail||'처리하지 못했습니다.';return}}location.href=d.redirect||'/';}})</script></body></html>"""


initialize_auth()


@app.on_event("startup")
def start_background_schedulers() -> None:
    ensure_dirs()
    threading.Thread(target=backup_scheduler, daemon=True, name="minecraft-backup-scheduler").start()
    ensure_restart_scheduler_running()
    ensure_discord_monitor_running()


@app.on_event("shutdown")
def stop_background_schedulers() -> None:
    RESTART_SCHEDULER_STOP.set()
    DISCORD_MONITOR_STOP.set()
    if RESTART_SCHEDULER_THREAD and RESTART_SCHEDULER_THREAD.is_alive():
        RESTART_SCHEDULER_THREAD.join(timeout=6)
    if DISCORD_MONITOR_THREAD and DISCORD_MONITOR_THREAD.is_alive():
        DISCORD_MONITOR_THREAD.join(timeout=6)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return RedirectResponse("/") if current_user(request) else HTMLResponse(login_html())


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return HTMLResponse(login_html(True))


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response):
    auth = read_json(AUTH_FILE, {})
    if payload.username != auth.get("username") or not secrets.compare_digest(hash_password(payload.password, auth.get("salt", "")), auth.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    sessions = read_json(SESSIONS_FILE, {})
    token = secrets.token_urlsafe(32)
    sessions[token] = {"username": payload.username, "created_at": datetime.now().isoformat()}
    write_json(SESSIONS_FILE, sessions)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=604800)
    return {"status": "ok", "redirect": "/change-password" if auth.get("must_change_password", True) else "/"}


@app.post("/api/auth/change-password")
def change_password(payload: ChangePasswordRequest, request: Request):
    require_auth(request)
    if not payload.new_password:
        raise HTTPException(status_code=400, detail="새 비밀번호를 입력해 주세요.")
    auth = read_json(AUTH_FILE, {})
    salt = secrets.token_hex(16)
    auth.update({"salt": salt, "password_hash": hash_password(payload.new_password, salt), "must_change_password": False})
    write_json(AUTH_FILE, auth)
    return {"status": "ok", "redirect": "/"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    sessions = read_json(SESSIONS_FILE, {})
    sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    write_json(SESSIONS_FILE, sessions)
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.post("/api/panel/update")
def request_panel_update(request: Request, tasks: BackgroundTasks):
    global PANEL_UPDATE_ACTIVE

    require_auth(request)
    current_status = read_panel_update_status()
    if current_status.get("status") in {"checking", "downloading", "restarting"}:
        if PANEL_UPDATE_ACTIVE or is_panel_updater_running():
            return {
                "status": current_status.get("status"),
                "message": current_status.get("message"),
            }
        write_panel_update_status(
            "failed",
            "이전에 중단된 구동기 업데이트 작업을 정리했습니다. 업데이트를 다시 시작합니다.",
            progress=int(current_status.get("progress") or 0),
        )

    with PANEL_UPDATE_LOCK:
        if PANEL_UPDATE_ACTIVE:
            return {
                "status": "running",
                "message": "이미 TechTim 구동기 업데이트 작업이 실행 중입니다.",
            }
        PANEL_UPDATE_ACTIVE = True

    write_panel_update_status(
        "checking",
        "TechTim 구동기 업데이트 확인 작업을 시작합니다.",
        progress=2,
    )
    tasks.add_task(panel_update_job)
    return {
        "status": "started",
        "message": "TechTim 구동기 업데이트 확인을 시작했습니다.",
    }


@app.get("/api/panel/update/status")
def panel_update_status(request: Request):
    require_auth(request)
    return read_panel_update_status()


@app.get("/api/panel/update/check")
def panel_update_check(request: Request):
    require_auth(request)
    return panel_update_check_payload()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    if read_json(AUTH_FILE, {}).get("must_change_password", True):
        return RedirectResponse("/change-password")
    html = (APP_DIR / "static" / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("{{PANEL_VERSION}}", PANEL_VERSION)
        .replace("{{ASSET_VERSION}}", STATIC_ASSET_VERSION)
    )


@app.post("/api/install")
def request_install(request: Request, tasks: BackgroundTasks):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 기동 중에는 엔진을 설치할 수 없습니다.")
    config = read_config()
    runtime_image = runtime_image_for_config(config)
    global INSTALL_ACTIVE
    with INSTALL_LOCK:
        if installed(config):
            raise HTTPException(status_code=409, detail="선택한 Java 버전의 게임 엔진이 이미 설치되어 있습니다.")
        if INSTALL_ACTIVE:
            raise HTTPException(status_code=409, detail="설치 작업이 이미 진행 중입니다.")
        INSTALL_ACTIVE = True
    tasks.add_task(install_job, runtime_image)
    return {"status": "started"}


@app.get("/api/install/status")
def install_status(request: Request):
    require_auth(request)
    status = INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip() if INSTALL_STATUS_FILE.exists() else "not_started"
    is_installed = installed()
    if status == "completed" and not is_installed:
        status = "not_started"
    return {"status": status, "installed": is_installed, "install_locked": is_installed}


@app.get("/api/install/log")
def install_log(request: Request):
    require_auth(request)
    return {"log": clean_log(INSTALL_LOG_FILE.read_text(encoding="utf-8")) if INSTALL_LOG_FILE.exists() else ""}


@app.get("/api/config")
def get_config(request: Request):
    require_auth(request)
    config = read_config()
    return {
        "config": config,
        "locked": server_running(),
        "engine_installed": installed(config),
        "types": sorted(SERVER_TYPES),
    }


@app.get("/api/minecraft/versions")
def minecraft_versions(request: Request):
    require_auth(request)
    return minecraft_version_payload()


@app.post("/api/config")
def save_config(payload: ConfigRequest, request: Request):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 설정을 변경할 수 없습니다.")
    config = normalize_config(payload.model_dump())
    validate_start(config)
    write_config(config)
    return {"status": "ok", "message": "설정이 저장되었습니다."}


@app.get("/api/server-icon")
def get_server_icon(request: Request):
    require_auth(request)
    if not SERVER_ICON_FILE.is_file():
        raise HTTPException(status_code=404, detail="등록된 서버 아이콘이 없습니다.")
    return FileResponse(
        SERVER_ICON_FILE,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/server-icon")
async def upload_server_icon(request: Request, icon: UploadFile = File(...)):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 서버 아이콘을 변경할 수 없습니다.")
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버 아이콘을 변경할 수 없습니다.")
    payload = await icon.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PNG 파일은 2MB 이하여야 합니다.")
    if (
        len(payload) < 24
        or payload[:8] != b"\x89PNG\r\n\x1a\n"
        or payload[12:16] != b"IHDR"
    ):
        raise HTTPException(status_code=400, detail="올바른 PNG 파일이 아닙니다.")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if (width, height) != (64, 64):
        raise HTTPException(status_code=400, detail="서버 아이콘은 64×64 PNG여야 합니다.")
    ensure_dirs()
    temporary = SERVER_ICON_FILE.with_suffix(".png.uploading")
    try:
        temporary.write_bytes(payload)
        temporary.replace(SERVER_ICON_FILE)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return {"status": "saved", "message": "서버 아이콘이 저장되었습니다."}


@app.delete("/api/server-icon")
def delete_server_icon(request: Request):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 서버 아이콘을 삭제할 수 없습니다.")
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버 아이콘을 삭제할 수 없습니다.")
    SERVER_ICON_FILE.unlink(missing_ok=True)
    return {"status": "deleted", "message": "서버 아이콘이 삭제되었습니다."}


@app.post("/api/server/start")
def start_server(payload: StartServerRequest, request: Request):
    require_auth(request)
    if restart_operation_active():
        raise HTTPException(status_code=409, detail="예약 재시작이 완료된 후 서버를 시작해주세요.")
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버를 시작할 수 없습니다.")
    if not payload.eula_accepted:
        raise HTTPException(status_code=400, detail="Minecraft EULA에 동의해야 서버를 시작할 수 있습니다.")
    if server_running():
        raise HTTPException(status_code=409, detail="이미 서버가 동작 중입니다.")
    config = read_config()
    if not installed(config):
        raise HTTPException(status_code=400, detail="먼저 서버 설치를 진행해주세요.")
    validate_start(config)
    runtime_image = runtime_image_for_config(config)
    environment = runtime_environment(config)
    client = docker_client()
    old = get_container()
    if old:
        old.reload()
        if old.status in STOPPABLE_SERVER_STATUSES:
            raise HTTPException(status_code=409, detail="이미 서버가 동작 중입니다.")
    CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    if old:
        if old.status in {"created", "exited"} and container_matches_runtime(old, runtime_image, environment):
            old.update(restart_policy={"Name": "unless-stopped"})
            append_log(CONTROL_LOG_FILE, "기존 서버 컨테이너와 영구 저장 데이터를 확인했습니다.")
            append_log(CONTROL_LOG_FILE, "재설치 없이 기존 Minecraft 서버를 시작합니다.")
            old.start()
            append_log(CONTROL_LOG_FILE, f"Minecraft 서버 시작 요청 완료: {old.short_id}")
            notify_discord_event(
                "server_start",
                "Minecraft 서버 시작",
                "기존 서버 데이터와 게임 컨테이너를 사용해 서버 시작을 요청했습니다.",
            )
            return {
                "status": "started",
                "message": "기존 Minecraft 서버를 시작했습니다.",
                "reused": True,
            }
        append_log(CONTROL_LOG_FILE, "변경된 서버 설정을 적용하기 위해 게임 컨테이너만 다시 구성합니다.")
        append_log(CONTROL_LOG_FILE, "월드와 서버 파일이 저장된 /data 데이터는 그대로 유지됩니다.")
        old.remove(force=True)
    ensure_dirs()
    signature = runtime_config_signature(runtime_image, environment)
    container = client.containers.run(
        runtime_image,
        name=SERVER_CONTAINER,
        detach=True,
        environment=environment,
        volumes={str(HOST_SERVER_DIR): {"bind": "/data", "mode": "rw"}},
        ports={"25565/tcp": SERVER_PORT},
        restart_policy={"Name": "unless-stopped"},
        labels={RUNTIME_CONFIG_LABEL: signature},
        stdin_open=True,
        tty=True,
    )
    if not old:
        append_log(CONTROL_LOG_FILE, "영구 저장 폴더를 연결하고 Minecraft 서버를 처음 시작합니다.")
    append_log(CONTROL_LOG_FILE, f"Minecraft 서버 시작 요청 완료: {container.short_id}")
    notify_discord_event(
        "server_start",
        "Minecraft 서버 시작",
        "Minecraft 게임 컨테이너 시작을 요청했습니다.",
    )
    return {
        "status": "started",
        "message": "Minecraft 서버를 시작했습니다.",
        "reused": False,
    }


@app.post("/api/server/stop")
def stop_server(request: Request):
    require_auth(request)
    if restart_operation_active():
        raise HTTPException(status_code=409, detail="예약 재시작이 완료된 후 서버를 중지해주세요.")
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업이 끝난 후 서버를 중지해주세요.")
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="생성된 서버 컨테이너가 없습니다.")
    container.reload()
    if container.status not in STOPPABLE_SERVER_STATUSES:
        return {"status": "stopped", "message": "Minecraft 서버가 이미 종료되어 있습니다."}
    append_log(CONTROL_LOG_FILE, "Minecraft 서버 즉시 종료를 요청했습니다.")
    suppress_discord_runtime_alerts()
    try:
        try:
            container.stop(timeout=0)
        except docker.errors.DockerException:
            pass
        container.reload()
        if container.status in STOPPABLE_SERVER_STATUSES:
            container.update(restart_policy={"Name": "no"})
            container.kill(signal="SIGKILL")
    except docker.errors.DockerException as error:
        raise HTTPException(status_code=500, detail=f"서버 즉시 종료에 실패했습니다: {error}") from error
    append_log(CONTROL_LOG_FILE, "Minecraft 서버가 즉시 종료되었습니다.")
    notify_discord_event(
        "server_stop",
        "Minecraft 서버 중지",
        "관리자 요청에 따라 게임 컨테이너를 종료했습니다.",
    )
    return {"status": "stopped", "message": "Minecraft 서버가 즉시 종료되었습니다."}


@app.post("/api/server/restart")
def restart_server(request: Request):
    require_auth(request)
    if restart_operation_active():
        raise HTTPException(status_code=409, detail="예약 재시작이 이미 진행 중입니다.")
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업이 끝난 후 서버를 재시작해주세요.")
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="먼저 서버를 시작해주세요.")
    suppress_discord_runtime_alerts()
    container.restart(timeout=60)
    CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    append_log(CONTROL_LOG_FILE, "Minecraft 서버를 재시작했습니다.")
    notify_discord_event(
        "server_restart",
        "Minecraft 서버 재시작",
        "관리자 요청에 따라 게임 컨테이너를 재시작했습니다.",
    )
    return {"status": "restarted"}


@app.post("/api/server/delete")
def delete_server(request: Request):
    require_auth(request)
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버 데이터를 삭제할 수 없습니다.")
    if server_running():
        raise HTTPException(status_code=409, detail="서버 기동 중에는 서버를 삭제할 수 없습니다.")
    suppress_discord_runtime_alerts()
    global INSTALL_ACTIVE
    with INSTALL_LOCK:
        if INSTALL_ACTIVE:
            raise HTTPException(status_code=409, detail="엔진 설치 중에는 서버를 삭제할 수 없습니다.")
        INSTALL_ACTIVE = True

    try:
        client = docker_client()
        try:
            container = client.containers.get(SERVER_CONTAINER)
        except docker.errors.NotFound:
            container = None
        if container:
            container.remove(force=True)

        if SERVER_DIR.is_symlink() or SERVER_DIR.is_file():
            SERVER_DIR.unlink()
        elif SERVER_DIR.exists():
            shutil.rmtree(SERVER_DIR)
        SERVER_DIR.mkdir(parents=True, exist_ok=True)

        for path in (
            CONFIG_FILE,
            INSTALL_LOG_FILE,
            INSTALL_STATUS_FILE,
            INSTALL_MARKER_FILE,
            CONTROL_LOG_FILE,
        ):
            path.unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"서버 데이터 삭제에 실패했습니다: {exc}") from exc
    finally:
        with INSTALL_LOCK:
            INSTALL_ACTIVE = False

    return {
        "status": "deleted",
        "message": "서버 데이터가 모두 삭제되었습니다. 엔진 설치부터 다시 진행해주세요.",
    }


@app.get("/api/server/status")
def server_status(request: Request):
    require_auth(request)
    container = get_container()
    status = "not_created"
    if container:
        container.reload()
        status = container.status
    return {
        "status": status,
        "running": status == "running",
        "stoppable": status in STOPPABLE_SERVER_STATUSES,
        "installed": installed(),
    }


@app.get("/api/server/resources")
def server_resources(request: Request):
    require_auth(request)
    ensure_dirs()
    disk = shutil.disk_usage(SERVER_DIR)
    cpu_percent, cpu_threads = os_cpu_usage()
    memory = os_memory_usage()
    resources: dict[str, Any] = {
        "running": False,
        "cpu_percent": cpu_percent,
        "cpu_threads": cpu_threads,
        **memory,
        "game_memory_percent": 0.0,
        "game_memory_used": 0,
        "game_memory_limit": 0,
        "disk_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0.0,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "network_received_per_second": 0,
        "network_sent_per_second": 0,
        "public_ip": public_server_ip(),
        "server_port": SERVER_PORT,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    container = get_container()
    if not container:
        return resources
    try:
        container.reload()
        if container.status != "running":
            return resources
        resources["running"] = True
        resources.update(container_resource_usage(container))
    except Exception as error:
        resources["error"] = clean_log(str(error))
    return resources


@app.get("/api/server/game-metrics")
def server_game_metrics(request: Request):
    require_auth(request)
    config = read_config()
    container = get_container()
    if not container:
        return empty_game_metrics(config)
    try:
        container.reload()
        if container.status != "running":
            return empty_game_metrics(config)
        return cached_game_metrics(container, config)
    except Exception as error:
        payload = empty_game_metrics(
            config,
            status="error",
            message=f"Minecraft 성능 지표 조회 실패: {clean_log(str(error))}",
        )
        payload["uptime_seconds"] = container_uptime_seconds(
            str(((getattr(container, "attrs", {}) or {}).get("State") or {}).get("StartedAt") or "")
        )
        return payload


def read_server_log() -> str:
    container = get_container()
    logs = ""
    if container:
        try:
            logs = clean_log(container.logs(stdout=True, stderr=True, tail=500).decode("utf-8", errors="replace"))
        except Exception as exc:
            logs = f"서버 로그 조회 실패: {exc}"
    control = clean_log(CONTROL_LOG_FILE.read_text(encoding="utf-8")) if CONTROL_LOG_FILE.exists() else ""
    if control:
        logs = f"[패널 제어 로그]\n{control.rstrip()}\n\n{logs.lstrip()}".strip()
    return logs


@app.get("/api/server/log")
def server_log(request: Request):
    require_auth(request)
    return {"log": read_server_log()}


@app.get("/api/log")
def combined_log(request: Request):
    require_auth(request)
    install = clean_log(INSTALL_LOG_FILE.read_text(encoding="utf-8")).strip() if INSTALL_LOG_FILE.exists() else ""
    server = read_server_log().strip()
    return {"log": "\n\n".join(part for part in (install, server) if part)}


@app.post("/api/server/command")
def send_server_command(payload: ServerCommandRequest, request: Request):
    require_auth(request)
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="먼저 서버를 시작해주세요.")
    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=409, detail="서버가 실행 중일 때만 명령어를 전송할 수 있습니다.")

    command = payload.command.strip().lstrip("/").strip()
    if not command or "\n" in command or "\r" in command or "\0" in command:
        raise HTTPException(status_code=400, detail="한 줄짜리 Minecraft 명령어를 입력해주세요.")

    try:
        send_native_console_command(container, command)
    except (docker.errors.DockerException, OSError, RuntimeError) as error:
        raise HTTPException(status_code=500, detail=f"Native 콘솔 명령 전송에 실패했습니다: {error}") from error

    return {
        "status": "sent",
        "message": "Native 서버 콘솔로 명령어를 전송했습니다.",
        "output": "",
    }


@app.get("/api/players")
def players_status(request: Request):
    require_auth(request)
    config = read_config()
    container = get_container()
    running = False
    online_players: list[str] = []
    error = ""
    if container:
        try:
            container.reload()
            running = container.status == "running"
            if running:
                result = container.exec_run(["rcon-cli", "list"], stdout=True, stderr=True)
                output = result.output.decode("utf-8", errors="replace") if isinstance(result.output, bytes) else str(result.output or "")
                if result.exit_code == 0:
                    online_players = parse_online_players(output)
                else:
                    error = clean_log(output).strip() or "접속자 목록을 불러오지 못했습니다."
        except docker.errors.DockerException as exc:
            error = f"접속자 조회 실패: {clean_log(str(exc))}"

    banned_players = []
    for entry in player_entries("banned-players.json"):
        name = str(entry.get("name") or "").strip()
        if name:
            banned_players.append({"name": name, "reason": str(entry.get("reason") or "").strip()})
    banned_players.sort(key=lambda entry: entry["name"].lower())
    return {
        "running": running,
        "online": online_players,
        "max_players": config["MaxPlayers"],
        "ops": player_names("ops.json"),
        "whitelist": player_names("whitelist.json"),
        "banned": banned_players,
        "error": error,
    }


@app.post("/api/players/action")
def player_action(payload: PlayerActionRequest, request: Request):
    require_auth(request)
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="먼저 서버를 시작해주세요.")
    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=409, detail="서버가 실행 중일 때만 플레이어를 관리할 수 있습니다.")

    action = payload.action.strip().lower()
    player = payload.player.strip()
    raw_reason = payload.reason.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        raise HTTPException(status_code=400, detail="플레이어명은 영문, 숫자, 밑줄을 사용해 16자 이내로 입력해주세요.")
    if any(character in raw_reason for character in ("\n", "\r", "\0")):
        raise HTTPException(status_code=400, detail="사유는 한 줄로 입력해주세요.")
    reason = re.sub(r"\s+", " ", raw_reason)

    commands = {
        "op": f"op {player}",
        "deop": f"deop {player}",
        "whitelist_add": f"whitelist add {player}",
        "whitelist_remove": f"whitelist remove {player}",
        "kick": f"kick {player}" + (f" {reason}" if reason else ""),
        "ban": f"ban {player}" + (f" {reason}" if reason else ""),
        "pardon": f"pardon {player}",
    }
    command = commands.get(action)
    if not command:
        raise HTTPException(status_code=400, detail="지원하지 않는 플레이어 관리 작업입니다.")

    try:
        send_native_console_command(container, command)
    except (docker.errors.DockerException, OSError, RuntimeError) as error:
        raise HTTPException(status_code=500, detail=f"Native 플레이어 관리 명령 전송에 실패했습니다: {error}") from error

    if action in {"op", "deop"}:
        update_config_player_list("Ops", player, action == "op")
    elif action in {"whitelist_add", "whitelist_remove"}:
        update_config_player_list("Whitelist", player, action == "whitelist_add")
    messages = {
        "op": f"{player} 플레이어에게 OP 권한을 부여했습니다.",
        "deop": f"{player} 플레이어의 OP 권한을 해제했습니다.",
        "whitelist_add": f"{player} 플레이어를 화이트리스트에 추가했습니다.",
        "whitelist_remove": f"{player} 플레이어를 화이트리스트에서 제거했습니다.",
        "kick": f"{player} 플레이어를 서버에서 추방했습니다.",
        "ban": f"{player} 플레이어의 접속을 차단했습니다.",
        "pardon": f"{player} 플레이어의 접속 차단을 해제했습니다.",
    }
    return {"status": "sent", "message": messages[action], "output": ""}


@app.get("/api/backups")
def backups_status(request: Request):
    require_auth(request)
    ensure_dirs()
    with BACKUP_LOCK:
        active = BACKUP_ACTIVE
    container = get_container()
    server_active = False
    if container:
        try:
            container.reload()
            server_active = container.status in STOPPABLE_SERVER_STATUSES
        except docker.errors.DockerException:
            server_active = False
    return {
        "config": read_backup_config(),
        "status": read_backup_status(),
        "active": active,
        "server_active": server_active,
        "backups": list_backup_archives(),
    }


@app.post("/api/backups/config")
def save_backup_config(payload: BackupConfigRequest, request: Request):
    require_auth(request)
    if not claim_backup_operation():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업이 진행 중입니다.")
    try:
        now = datetime.now(KST)
        existing = read_backup_config()
        config = {
            "enabled": payload.enabled,
            "interval_hours": payload.interval_hours,
            "retention_count": payload.retention_count,
            "last_backup_at": existing["last_backup_at"],
            "next_run_at": (
                now + timedelta(hours=payload.interval_hours)
            ).isoformat(timespec="seconds") if payload.enabled else "",
        }
        write_backup_config(config)
        prune_backup_archives(payload.retention_count)
        return {
            "status": "ok",
            "message": "자동 백업 설정이 저장되었습니다.",
            "config": config,
        }
    finally:
        release_backup_operation()


@app.post("/api/backups/run")
def run_backup_now(request: Request):
    require_auth(request)
    ensure_dirs()
    if not any(SERVER_DIR.iterdir()):
        raise HTTPException(status_code=409, detail="백업할 Minecraft 서버 데이터가 없습니다.")
    if not start_backup("manual"):
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업이 이미 진행 중입니다.")
    return {"status": "started", "message": "즉시 백업을 시작했습니다."}


@app.post("/api/backups/{filename}/restore")
def restore_backup(filename: str, request: Request):
    require_auth(request)
    container = get_container()
    if container:
        container.reload()
        if container.status in STOPPABLE_SERVER_STATUSES:
            raise HTTPException(status_code=409, detail="백업 복원은 Minecraft 서버를 중지한 후 진행해주세요.")
    source = backup_archive_path(filename)
    if not start_restore(source):
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업이 이미 진행 중입니다.")
    return {"status": "started", "message": "백업 복원을 시작했습니다."}


@app.get("/api/backups/{filename}/download")
def download_backup(filename: str, request: Request):
    require_auth(request)
    path = backup_archive_path(filename)
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@app.post("/api/backups/{filename}/delete")
def delete_backup(filename: str, request: Request):
    require_auth(request)
    with BACKUP_LOCK:
        if BACKUP_ACTIVE:
            raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 백업 파일을 삭제할 수 없습니다.")
    path = backup_archive_path(filename)
    path.unlink()
    return {"status": "deleted", "message": "백업 파일을 삭제했습니다."}


@app.get("/api/restart-schedule")
def get_restart_schedule(request: Request):
    require_auth(request)
    return {
        "status": "ok",
        "schedule": restart_schedule_response(),
    }


@app.post("/api/restart-schedule")
def save_restart_schedule(payload: RestartScheduleRequest, request: Request):
    require_auth(request)
    existing = read_restart_schedule()
    changed = (
        existing["enabled"] != payload.enabled
        or existing["restart_time"] != payload.restart_time
    )
    schedule = {
        **existing,
        "enabled": payload.enabled,
        "restart_time": payload.restart_time,
    }
    if changed:
        schedule["last_result"] = "not_run"
        schedule["last_message"] = "새 예약이 저장되었습니다."
        schedule["last_run_date"] = ""
        schedule["last_run_at"] = ""
    saved = write_restart_schedule(schedule)
    if saved["enabled"]:
        ensure_restart_scheduler_running()
    state = "활성화" if saved["enabled"] else "비활성화"
    return {
        "status": "ok",
        "message": f"게임 서버 예약 재시작이 {state}되었습니다.",
        "schedule": restart_schedule_response(saved),
    }


@app.get("/api/discord")
def get_discord_config(request: Request):
    require_auth(request)
    return {
        "status": "ok",
        "config": discord_config_response(),
    }


@app.post("/api/discord")
def save_discord_config(payload: DiscordConfigRequest, request: Request):
    require_auth(request)
    existing = read_discord_config()
    webhook_url = existing["webhook_url"]
    if payload.clear_webhook:
        webhook_url = ""
    elif payload.webhook_url.strip():
        try:
            webhook_url = normalize_webhook_url(payload.webhook_url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    if payload.enabled and not webhook_url:
        raise HTTPException(status_code=400, detail="Discord 연동을 사용하려면 웹훅 URL을 먼저 등록해주세요.")

    saved = write_discord_config({
        "enabled": payload.enabled,
        "webhook_url": webhook_url,
        "username": payload.username,
        "notify_server_start": payload.notify_server_start,
        "notify_server_stop": payload.notify_server_stop,
        "notify_server_restart": payload.notify_server_restart,
        "notify_backup": payload.notify_backup,
        "notify_errors": payload.notify_errors,
    })
    return {
        "status": "ok",
        "message": "Discord 연동 설정이 저장되었습니다.",
        "config": discord_config_response(saved),
    }


@app.post("/api/discord/test")
def test_discord_webhook(request: Request):
    require_auth(request)
    config = read_discord_config()
    if not config["webhook_url"]:
        raise HTTPException(status_code=400, detail="테스트할 Discord 웹훅 URL을 먼저 저장해주세요.")
    try:
        deliver_discord_event(
            config,
            event="test",
            title="Discord 연동 테스트 성공",
            message="TechTim Minecraft Server Panel과 Discord 채널이 정상적으로 연결되었습니다.",
            fields=[{"name": "알림 상태", "value": "정상", "inline": True}],
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=clean_log(str(error))) from error
    return {"status": "sent", "message": "Discord 테스트 메시지를 전송했습니다."}


@app.get("/api/files")
def list_files(request: Request, path: str = ""):
    require_auth(request)
    target = resolve_path(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="폴더를 찾을 수 없습니다.")
    entries = []
    for child in target.iterdir():
        try:
            stat = child.stat()
            entries.append({"name": child.name, "path": relative_path(child), "type": "dir" if child.is_dir() else "file", "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")})
        except OSError:
            continue
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return {"root": "/data", "path": relative_path(target), "parent": relative_path(target.parent) if target != SERVER_DIR.resolve() else "", "write_locked": server_running(), "entries": entries}


def require_file_write() -> None:
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버 파일을 변경할 수 없습니다.")
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 파일을 변경할 수 없습니다.")


def require_file_download() -> None:
    if backup_operation_active():
        raise HTTPException(status_code=409, detail="백업 또는 복원 작업 중에는 서버 파일을 다운로드할 수 없습니다.")
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 서버 파일을 다운로드할 수 없습니다.")


@app.get("/api/files/download")
def download_file(request: Request, path: str):
    require_auth(request)
    require_file_download()
    target = resolve_download_target(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(str(target), filename=target.name)


@app.post("/api/files/download-selected")
def download_selected_files(payload: FileDownloadRequest, request: Request):
    require_auth(request)
    require_file_download()
    targets: list[Path] = []
    seen: set[Path] = set()
    for relative in payload.paths:
        target = resolve_download_target(relative)
        if target not in seen:
            targets.append(target)
            seen.add(target)
    try:
        archive_path, download_name = create_selected_download_archive(targets)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise HTTPException(status_code=500, detail=f"선택 항목 ZIP 생성 중 오류가 발생했습니다: {error}") from error
    return FileResponse(
        path=str(archive_path),
        filename=download_name,
        media_type="application/zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.post("/api/files/upload")
async def upload_file(request: Request, path: str = "", file: UploadFile = File(...)):
    require_auth(request)
    require_file_write()
    parent = resolve_path(path)
    if not parent.is_dir():
        raise HTTPException(status_code=404, detail="업로드 폴더를 찾을 수 없습니다.")
    name = Path(file.filename or "upload.bin").name
    if name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="올바른 파일 이름이 아닙니다.")
    target = parent / name
    with target.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    return {"status": "ok"}


@app.post("/api/files/upload-folder")
async def upload_folder(
    request: Request,
    path: str = "",
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(...),
):
    require_auth(request)
    require_file_write()
    parent = resolve_path(path)
    if not parent.is_dir():
        raise HTTPException(status_code=404, detail="업로드 폴더를 찾을 수 없습니다.")
    if not files or len(files) != len(relative_paths):
        raise HTTPException(status_code=400, detail="업로드 파일과 상대 경로 정보가 일치하지 않습니다.")

    upload_targets: list[tuple[UploadFile, Path]] = []
    seen_targets: set[Path] = set()
    for upload, upload_path in zip(files, relative_paths):
        target = resolve_folder_upload_target(parent, upload_path)
        if target in seen_targets:
            raise HTTPException(status_code=400, detail="폴더 업로드에 중복된 파일 경로가 포함되어 있습니다.")
        seen_targets.add(target)
        upload_targets.append((upload, target))

    for upload, target in upload_targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            shutil.copyfileobj(upload.file, output)

    return {"status": "ok", "uploaded_count": len(upload_targets)}


@app.post("/api/files/mkdir")
def make_dir(payload: CreateDirRequest, request: Request):
    require_auth(request)
    require_file_write()
    name = Path(payload.name).name.strip()
    if name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="올바른 폴더 이름이 아닙니다.")
    (resolve_path(payload.path) / name).mkdir()
    return {"status": "ok"}


@app.post("/api/files/delete")
def delete_path(payload: DeleteRequest, request: Request):
    require_auth(request)
    require_file_write()
    target = resolve_path(payload.path)
    if target == SERVER_DIR.resolve():
        raise HTTPException(status_code=400, detail="루트 폴더는 삭제할 수 없습니다.")
    if target.is_dir():
        target.rmdir()
    elif target.exists():
        target.unlink()
    else:
        raise HTTPException(status_code=404, detail="항목을 찾을 수 없습니다.")
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "game": "minecraft", "version": PANEL_VERSION}
