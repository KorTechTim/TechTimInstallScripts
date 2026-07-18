from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time

import docker
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


app = FastAPI(title="TechTim Minecraft Server Panel")
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

PANEL_VERSION = os.getenv("PANEL_VERSION", "1.0.0")
STATIC_ASSET_VERSION = hashlib.sha256(
    (APP_DIR / "static" / "app.css").read_bytes()
    + (APP_DIR / "static" / "app.js").read_bytes()
).hexdigest()[:12]
MINECRAFT_VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/minecraft/data"))
SERVER_DIR = DATA_DIR / "server"
HOST_SERVER_DIR = HOST_DATA_DIR / "server"
SERVER_CONTAINER = os.getenv("MINECRAFT_SERVER_CONTAINER", "minecraft-server")
RUNTIME_IMAGE = os.getenv("MINECRAFT_RUNTIME_IMAGE", "itzg/minecraft-server:latest")
PANEL_CONTAINER_NAME = os.getenv("PANEL_CONTAINER_NAME", "minecraft-panel")
PANEL_PROXY_CONTAINER = os.getenv("PANEL_PROXY_CONTAINER", "minecraft-panel-proxy")
PANEL_IMAGE = os.getenv("PANEL_IMAGE", "ghcr.io/kortechtim/minecraft-panel:latest")
SERVER_PORT = int(os.getenv("SERVER_PORT", "25565"))
PULL_HEARTBEAT_SECONDS = max(2, int(os.getenv("DOCKER_PULL_HEARTBEAT_SECONDS", "10")))
INSTALL_CODE = os.getenv("INSTALL_CODE", "").strip()
CURSEFORGE_PROXY_URL = os.getenv(
    "CURSEFORGE_PROXY_URL",
    "https://www.techtim.kr/api/curseforge",
).strip()

AUTH_FILE = DATA_DIR / "auth.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
CONFIG_FILE = DATA_DIR / "minecraft-config.json"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"
INSTALL_MARKER_FILE = DATA_DIR / "install-request.txt"
CONTROL_LOG_FILE = DATA_DIR / "server-control.log"
PANEL_UPDATE_STATUS_FILE = DATA_DIR / "panel-update-status.json"
SESSION_COOKIE = "techtim_session"
INSTALL_LOCK = threading.Lock()
INSTALL_ACTIVE = False
PANEL_UPDATE_LOCK = threading.Lock()
PANEL_UPDATE_ACTIVE = False
VERSION_CACHE_LOCK = threading.Lock()
VERSION_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
RESOURCE_LOCK = threading.Lock()
RESOURCE_NETWORK_SAMPLE: dict[str, Any] = {
    "container_id": "",
    "sampled_at": 0.0,
    "received": 0,
    "sent": 0,
}
KST = timezone(timedelta(hours=9), name="KST")
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_FRAGMENT_RE = re.compile(r"(?:\[(?:0|1|2|3|4|5|7|9|10[0-7]|[34][0-7])m)+")

SERVER_TYPES = {
    "VANILLA", "PAPER", "PURPUR", "SPIGOT", "FORGE", "NEOFORGE",
    "FABRIC", "QUILT",
}
MODPACK_URL_TYPES = {"FORGE", "NEOFORGE", "FABRIC"}
JAVA_VERSIONS = {"AUTO", "8", "11", "16", "17", "21", "25"}

FALLBACK_MINECRAFT_VERSIONS = [
    "26.2", "26.1.2", "26.1.1", "26.1",
    "1.21.11", "1.21.10", "1.21.9", "1.21.8", "1.21.7", "1.21.6",
    "1.21.5", "1.21.4", "1.21.3", "1.21.2", "1.21.1", "1.21",
    "1.20.6", "1.20.5", "1.20.4", "1.20.3", "1.20.2", "1.20.1", "1.20",
    "1.19.4", "1.19.3", "1.19.2", "1.19.1", "1.19",
    "1.18.2", "1.18.1", "1.18",
    "1.17.1", "1.17",
    "1.16.5", "1.16.4", "1.16.3", "1.16.2", "1.16.1", "1.16",
]


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    new_password: str


class ServerCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)


class StartServerRequest(BaseModel):
    eula_accepted: bool = False


class ConfigRequest(BaseModel):
    Type: str = "PAPER"
    Version: str = "LATEST"
    JavaVersion: str = "AUTO"
    Memory: str = "4G"
    ServerName: str = "TechTim Minecraft Server"
    Motd: str = "TechTim Minecraft Server"
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
    ModrinthProjects: str = ""
    ModpackUrl: str = ""
    ModpackSource: str = "manual"
    CurseForgeProjectId: str = ""
    CurseForgeFileId: str = ""
    CurseForgeSlug: str = ""
    CurseForgeProjectName: str = ""
    CurseForgeFileName: str = ""
    CurseForgePageUrl: str = ""
    CurseForgeGameVersion: str = ""
    CurseForgeLoader: str = ""
    ExtraEnv: dict[str, Any] = Field(default_factory=dict)


class CreateDirRequest(BaseModel):
    path: str = ""
    name: str


class DeleteRequest(BaseModel):
    path: str


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_DIR.mkdir(parents=True, exist_ok=True)


def clean_log(value: str) -> str:
    text = ANSI_RE.sub("", str(value or ""))
    return ANSI_FRAGMENT_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


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


def releases_since_1_16(manifest: dict) -> list[str]:
    releases = []
    for version in manifest.get("versions") or []:
        if not isinstance(version, dict) or version.get("type") != "release":
            continue
        version_id = str(version.get("id") or "").strip()
        if not version_id or version_id in releases:
            continue
        releases.append(version_id)
        if version_id == "1.16":
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
        releases = releases_since_1_16(manifest)
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


def curseforge_proxy_request(action: str, params: dict[str, str]) -> dict:
    if not INSTALL_CODE:
        raise HTTPException(status_code=503, detail="패널에 Minecraft 설치 코드가 설정되지 않았습니다.")
    proxy_url = urlparse(CURSEFORGE_PROXY_URL)
    local_development_proxy = proxy_url.scheme == "http" and proxy_url.hostname in {"127.0.0.1", "localhost"}
    if proxy_url.scheme != "https" and not local_development_proxy:
        raise HTTPException(status_code=503, detail="CurseForge API 중계 주소가 올바르지 않습니다.")

    query = urlencode({"action": action, **{key: value for key, value in params.items() if value}})
    request = UrlRequest(
        f"{CURSEFORGE_PROXY_URL}?{query}",
        headers={
            "Accept": "application/json",
            "X-TechTim-Install-Code": INSTALL_CODE,
            "User-Agent": f"TechTim-Minecraft-Panel/{PANEL_VERSION}",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            detail = str(payload.get("detail") or "CurseForge API 중계 요청이 거부되었습니다.")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            detail = "CurseForge API 중계 요청이 거부되었습니다."
        raise HTTPException(status_code=error.code, detail=detail) from error
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail="CurseForge API에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.") from error


def normalize_config(raw: dict) -> dict:
    merged = {**default_config(), **(raw or {})}
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
    merged["ModpackUrl"] = str(merged.get("ModpackUrl") or "").strip()
    merged["ModpackSource"] = str(merged.get("ModpackSource") or "manual").strip().lower()
    if merged["ModpackSource"] not in {"manual", "curseforge"}:
        merged["ModpackSource"] = "manual"
    if merged["Type"] not in MODPACK_URL_TYPES:
        merged["ModpackSource"] = "manual"
    for key in (
        "CurseForgeProjectId", "CurseForgeFileId", "CurseForgeSlug",
        "CurseForgeProjectName", "CurseForgeFileName", "CurseForgePageUrl",
        "CurseForgeGameVersion", "CurseForgeLoader",
    ):
        merged[key] = str(merged.get(key) or "").strip()
    merged["ExtraEnv"] = merged.get("ExtraEnv") if isinstance(merged.get("ExtraEnv"), dict) else {}
    return merged


def read_config() -> dict:
    ensure_dirs()
    return normalize_config(read_json(CONFIG_FILE, {}))


def write_config(config: dict) -> None:
    ensure_dirs()
    write_json(CONFIG_FILE, normalize_config(config))


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


def container_resource_usage(container) -> dict[str, Any]:
    stats = container.stats(stream=False)
    cpu_stats = stats.get("cpu_stats") or {}
    previous_cpu_stats = stats.get("precpu_stats") or {}
    cpu_usage = cpu_stats.get("cpu_usage") or {}
    previous_cpu_usage = previous_cpu_stats.get("cpu_usage") or {}
    cpu_delta = float(cpu_usage.get("total_usage") or 0) - float(previous_cpu_usage.get("total_usage") or 0)
    system_delta = float(cpu_stats.get("system_cpu_usage") or 0) - float(previous_cpu_stats.get("system_cpu_usage") or 0)
    cpu_percent = max(0.0, min(100.0, cpu_delta / system_delta * 100)) if system_delta > 0 and cpu_delta >= 0 else 0.0

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
        "cpu_percent": round(cpu_percent, 1),
        "memory_percent": round(min(100.0, max(0.0, memory_percent)), 1),
        "memory_used": memory_used,
        "memory_limit": memory_limit,
        "network_received_per_second": round(received_per_second),
        "network_sent_per_second": round(sent_per_second),
    }


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
        "MODRINTH_PROJECTS": config["ModrinthProjects"],
    }
    curseforge_selected = config["ModpackSource"] == "curseforge" and bool(config["CurseForgePageUrl"])
    if curseforge_selected:
        env["TYPE"] = "AUTO_CURSEFORGE"
        optional["CF_PAGE_URL"] = config["CurseForgePageUrl"]
    elif config["Type"] in MODPACK_URL_TYPES:
        optional["GENERIC_PACK"] = config["ModpackUrl"]
    if not curseforge_selected and config["Type"] == "FORGE" and config["ModpackUrl"]:
        optional["FORGE_VERSION"] = "latest"
    for key, value in optional.items():
        if str(value or "").strip():
            env[key] = str(value).strip()
    if config["ModrinthProjects"].strip():
        env["MODRINTH_DOWNLOAD_DEPENDENCIES"] = "required"
    for key, value in config.get("ExtraEnv", {}).items():
        normalized_key = str(key).strip().upper()
        if normalized_key != "EULA" and re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_key) and value is not None:
            env[normalized_key] = str(value)
    return env


def validate_start(config: dict) -> None:
    if config["ModpackSource"] == "curseforge":
        page_url = config["CurseForgePageUrl"]
        if not re.fullmatch(
            r"https://www\.curseforge\.com/minecraft/modpacks/[a-z0-9-]+/files/[1-9]\d*",
            page_url,
            re.IGNORECASE,
        ):
            raise HTTPException(status_code=400, detail="CurseForge 검색에서 설치할 모드팩 버전을 다시 선택해주세요.")
    elif config["ModpackUrl"] and not re.match(r"^https?://\S+$", config["ModpackUrl"], re.IGNORECASE):
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
    global INSTALL_ACTIVE
    with INSTALL_LOCK:
        if INSTALL_MARKER_FILE.exists():
            raise HTTPException(status_code=409, detail="게임 엔진이 이미 설치되어 있습니다. 다시 설치하려면 서버를 먼저 삭제해주세요.")
        if INSTALL_ACTIVE:
            raise HTTPException(status_code=409, detail="설치 작업이 이미 진행 중입니다.")
        INSTALL_ACTIVE = True
    runtime_image = runtime_image_for_config(read_config())
    tasks.add_task(install_job, runtime_image)
    return {"status": "started"}


@app.get("/api/install/status")
def install_status(request: Request):
    require_auth(request)
    status = INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip() if INSTALL_STATUS_FILE.exists() else "not_started"
    is_installed = installed()
    if status == "completed" and not is_installed:
        status = "not_started"
    return {"status": status, "installed": is_installed, "install_locked": INSTALL_MARKER_FILE.exists()}


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
        "engine_installed": INSTALL_MARKER_FILE.exists(),
        "types": sorted(SERVER_TYPES),
    }


@app.get("/api/minecraft/versions")
def minecraft_versions(request: Request):
    require_auth(request)
    return minecraft_version_payload()


@app.get("/api/curseforge/search")
def curseforge_search(
    request: Request,
    query: str = "",
    game_version: str = "",
    mod_loader_type: str = "",
):
    require_auth(request)
    return curseforge_proxy_request("search", {
        "query": query.strip()[:80],
        "gameVersion": game_version.strip()[:32],
        "modLoaderType": mod_loader_type.strip()[:2],
    })


@app.get("/api/curseforge/projects/{project_id}/files")
def curseforge_project_files(
    project_id: int,
    request: Request,
    game_version: str = "",
    mod_loader_type: str = "",
):
    require_auth(request)
    if project_id <= 0:
        raise HTTPException(status_code=400, detail="올바른 CurseForge 프로젝트 ID가 필요합니다.")
    return curseforge_proxy_request("files", {
        "projectId": str(project_id),
        "gameVersion": game_version.strip()[:32],
        "modLoaderType": mod_loader_type.strip()[:2],
    })


@app.post("/api/config")
def save_config(payload: ConfigRequest, request: Request):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 설정을 변경할 수 없습니다.")
    config = normalize_config(payload.model_dump())
    if INSTALL_MARKER_FILE.exists() and config["JavaVersion"] != read_config()["JavaVersion"]:
        raise HTTPException(status_code=409, detail="엔진 설치 후에는 Java 버전을 변경할 수 없습니다. 서버 삭제 후 다시 설정해주세요.")
    validate_start(config)
    write_config(config)
    return {"status": "ok", "message": "설정이 저장되었습니다."}


@app.post("/api/server/start")
def start_server(payload: StartServerRequest, request: Request):
    require_auth(request)
    if not payload.eula_accepted:
        raise HTTPException(status_code=400, detail="Minecraft EULA에 동의해야 서버를 시작할 수 있습니다.")
    if server_running():
        raise HTTPException(status_code=409, detail="이미 서버가 동작 중입니다.")
    config = read_config()
    if not installed(config):
        raise HTTPException(status_code=400, detail="먼저 서버 설치를 진행해주세요.")
    validate_start(config)
    runtime_image = runtime_image_for_config(config)
    client = docker_client()
    old = get_container()
    if old:
        old.remove(force=True)
    ensure_dirs()
    CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    container = client.containers.run(
        runtime_image,
        name=SERVER_CONTAINER,
        detach=True,
        environment=runtime_environment(config),
        volumes={str(HOST_SERVER_DIR): {"bind": "/data", "mode": "rw"}},
        ports={"25565/tcp": SERVER_PORT},
        restart_policy={"Name": "unless-stopped"},
        stdin_open=True,
        tty=True,
    )
    append_log(CONTROL_LOG_FILE, f"Minecraft 서버 시작 요청 완료: {container.short_id}")
    return {"status": "started"}


@app.post("/api/server/stop")
def stop_server(request: Request):
    require_auth(request)
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="생성된 서버 컨테이너가 없습니다.")
    container.reload()
    if container.status != "running":
        return {"status": "stopped"}
    append_log(CONTROL_LOG_FILE, "Minecraft 서버 종료를 요청했습니다. 월드 저장 후 종료합니다.")
    container.stop(timeout=60)
    append_log(CONTROL_LOG_FILE, "Minecraft 서버가 정상 종료되었습니다.")
    return {"status": "stopped"}


@app.post("/api/server/restart")
def restart_server(request: Request):
    require_auth(request)
    container = get_container()
    if not container:
        raise HTTPException(status_code=404, detail="먼저 서버를 시작해주세요.")
    container.restart(timeout=60)
    CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    append_log(CONTROL_LOG_FILE, "Minecraft 서버를 재시작했습니다.")
    return {"status": "restarted"}


@app.post("/api/server/delete")
def delete_server(request: Request):
    require_auth(request)
    if server_running():
        raise HTTPException(status_code=409, detail="서버 기동 중에는 서버를 삭제할 수 없습니다.")
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
    return {"status": status, "running": status == "running", "installed": installed()}


@app.get("/api/server/resources")
def server_resources(request: Request):
    require_auth(request)
    ensure_dirs()
    disk = shutil.disk_usage(SERVER_DIR)
    resources: dict[str, Any] = {
        "running": False,
        "cpu_percent": 0.0,
        "memory_percent": 0.0,
        "memory_used": 0,
        "memory_limit": 0,
        "disk_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0.0,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "network_received_per_second": 0,
        "network_sent_per_second": 0,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    container = get_container()
    if not container:
        return resources
    try:
        container.reload()
        if container.status != "running":
            return resources
        resources.update(container_resource_usage(container))
        resources["running"] = True
    except Exception as error:
        resources["error"] = clean_log(str(error))
    return resources


@app.get("/api/server/log")
def server_log(request: Request):
    require_auth(request)
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
    return {"log": logs}


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
        result = container.exec_run(["rcon-cli", command], stdout=True, stderr=True)
    except docker.errors.DockerException as error:
        raise HTTPException(status_code=500, detail=f"명령어 전송에 실패했습니다: {error}") from error

    output = result.output.decode("utf-8", errors="replace") if isinstance(result.output, bytes) else str(result.output or "")
    output = clean_log(output).strip()
    append_log(CONTROL_LOG_FILE, f"[콘솔 명령] > {command}")
    if output:
        append_log(CONTROL_LOG_FILE, f"[명령 결과] {output[:4000]}")
    if result.exit_code != 0:
        raise HTTPException(status_code=500, detail=output or "Minecraft 서버가 명령어를 처리하지 못했습니다.")
    return {"status": "sent", "message": "명령어를 전송했습니다.", "output": output}


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
    if server_running():
        raise HTTPException(status_code=409, detail="서버 실행 중에는 파일을 변경할 수 없습니다.")


@app.get("/api/files/download")
def download_file(request: Request, path: str):
    require_auth(request)
    target = resolve_path(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(str(target), filename=target.name)


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
