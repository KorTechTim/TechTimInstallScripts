from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time
import zipfile

import docker

app = FastAPI(title="TechTim Palworld Server Panel")
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_FRAGMENT_RE = re.compile(r"(?:\[(?:0|1|2|3|4|5|7|9|10[0-7]|[34][0-7])m)+")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

GAME_CODE = os.getenv("GAME_CODE", "palworld")
PANEL_VERSION = os.getenv("PANEL_VERSION", "1.0.0")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/palworld/data"))

PALWORLD_SERVER_CONTAINER = os.getenv("PALWORLD_SERVER_CONTAINER", "palworld-server")
PALWORLD_RUNTIME_IMAGE = os.getenv(
    "PALWORLD_RUNTIME_IMAGE",
    "ghcr.io/pocketpairjp/palserver:v1.0.0.100427",
)
SERVER_PORT = int(os.getenv("SERVER_PORT", "8211"))
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
SERVER_STOP_GRACE_SECONDS = int(os.getenv("SERVER_STOP_GRACE_SECONDS", "5"))
DOCKER_PULL_HEARTBEAT_SECONDS = max(1, int(os.getenv("DOCKER_PULL_HEARTBEAT_SECONDS", "10")))

INSTALL_REQUEST_FILE = DATA_DIR / "install-request.txt"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"
SERVER_CONTROL_LOG_FILE = DATA_DIR / "server-control.log"
RESTART_SCHEDULE_FILE = DATA_DIR / "restart-schedule.json"
RUNTIME_HELPER_FILE = DATA_DIR / "palworld-runtime-helper.sh"
HOST_RUNTIME_HELPER_FILE = HOST_DATA_DIR / "palworld-runtime-helper.sh"

SAVED_ROOT_DIR = DATA_DIR / "server" / "Pal" / "Saved"
SAVED_WORLDS_DIR = SAVED_ROOT_DIR / "SaveGames"
SAVE_EXPORT_DIR = DATA_DIR / "uploads"

AUTH_FILE = DATA_DIR / "auth.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SESSION_COOKIE_NAME = "techtim_session"
INSTALL_JOB_LOCK = threading.Lock()
INSTALL_JOB_ACTIVE = False
RESTART_SCHEDULE_LOCK = threading.RLock()
RESTART_SCHEDULER_STOP = threading.Event()
RESTART_SCHEDULER_THREAD: threading.Thread | None = None
KST = timezone(timedelta(hours=9), name="KST")


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    new_password: str


class ConfigRequest(BaseModel):
    ServerName: str = "TechTim Palworld Server"
    ServerDescription: str = ""
    AdminPassword: str = ""
    ServerPassword: str = ""
    PublicPort: int = SERVER_PORT
    MaxPlayers: int = 32
    RCONEnabled: bool = False
    RCONPort: int = RCON_PORT
    AdvancedOptions: dict[str, Any] = Field(default_factory=dict)


class RestartScheduleRequest(BaseModel):
    enabled: bool = False
    restart_time: str = "04:00"


class FileExplorerCreateDirRequest(BaseModel):
    path: str = ""
    name: str


class FileExplorerDeleteRequest(BaseModel):
    path: str


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "server").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "backups").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    SAVED_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_WORLDS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_official_runtime_files() -> None:
    ensure_data_dirs()
    RUNTIME_HELPER_FILE.write_text(
        "#!/bin/sh\n"
        "sudo chown -R user:usergroup /pal/Package/Pal/Saved\n"
        "exec /bin/sh /pal/Package/PalServer.sh \"$@\"\n",
        encoding="utf-8",
    )
    RUNTIME_HELPER_FILE.chmod(0o755)


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()


def init_auth_if_needed() -> None:
    ensure_data_dirs()

    if AUTH_FILE.exists():
        return

    salt = secrets.token_hex(16)

    auth_data = {
        "username": "admin",
        "password_hash": password_hash("admin", salt),
        "salt": salt,
        "must_change_password": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    AUTH_FILE.write_text(
        json.dumps(auth_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if not SESSIONS_FILE.exists():
        SESSIONS_FILE.write_text("{}", encoding="utf-8")


def load_auth() -> dict:
    init_auth_if_needed()
    return json.loads(AUTH_FILE.read_text(encoding="utf-8"))


def save_auth(auth_data: dict) -> None:
    auth_data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    AUTH_FILE.write_text(
        json.dumps(auth_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def verify_password(password: str, auth_data: dict) -> bool:
    expected = auth_data.get("password_hash", "")
    salt = auth_data.get("salt", "")
    actual = password_hash(password, salt)
    return secrets.compare_digest(actual, expected)


def load_sessions() -> dict:
    ensure_data_dirs()

    if not SESSIONS_FILE.exists():
        SESSIONS_FILE.write_text("{}", encoding="utf-8")

    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_sessions(sessions: dict) -> None:
    SESSIONS_FILE.write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def create_session(username: str) -> str:
    sessions = load_sessions()
    token = secrets.token_urlsafe(32)

    sessions[token] = {
        "username": username,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    save_sessions(sessions)
    return token


def delete_session(token: str | None) -> None:
    if not token:
        return

    sessions = load_sessions()

    if token in sessions:
        del sessions[token]
        save_sessions(sessions)


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)

    if not token:
        return None

    sessions = load_sessions()
    session = sessions.get(token)

    if not session:
        return None

    return session.get("username")


def require_auth(request: Request) -> str:
    user = get_current_user(request)

    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return user


def must_change_password() -> bool:
    auth_data = load_auth()
    return bool(auth_data.get("must_change_password", True))


def sanitize_log_text(text: str) -> str:
    clean_text = ANSI_ESCAPE_RE.sub("", str(text or ""))
    clean_text = ANSI_FRAGMENT_RE.sub("", clean_text)
    return clean_text.replace("\r\n", "\n").replace("\r", "\n")


def write_log(message: str) -> None:
    ensure_data_dirs()
    now = datetime.now().isoformat(timespec="seconds")
    clean_message = sanitize_log_text(message)

    with INSTALL_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {clean_message}\n")


def pull_docker_image_with_progress(client, image: str) -> None:
    repository, tag = docker.utils.parse_repository_tag(image)
    tag = tag or "latest"
    layer_statuses: dict[str, str] = {}
    last_progress_log = 0.0
    pull_started_at = time.monotonic()
    last_event_at = time.monotonic()
    last_visible_log_at = time.monotonic()
    received_event = False
    pull_events: queue.Queue = queue.Queue()

    def consume_pull_events() -> None:
        try:
            for event in client.api.pull(
                repository,
                tag=tag,
                stream=True,
                decode=True,
            ):
                pull_events.put(("event", event))
        except Exception as pull_error:
            pull_events.put(("error", pull_error))
        finally:
            pull_events.put(("done", None))

    pull_thread = threading.Thread(target=consume_pull_events, daemon=True)
    pull_thread.start()

    while True:
        try:
            event_type, payload = pull_events.get(timeout=DOCKER_PULL_HEARTBEAT_SECONDS)
        except queue.Empty:
            now = time.monotonic()
            idle_seconds = int(now - last_event_at)
            elapsed_seconds = int(now - pull_started_at)
            elapsed_minutes, elapsed_remainder = divmod(elapsed_seconds, 60)
            write_log(
                "[docker] 이미지 다운로드 또는 압축 해제가 계속 진행 중입니다. "
                f"전체 경과: {elapsed_minutes}분 {elapsed_remainder}초, "
                f"마지막 Docker 응답: {idle_seconds}초 전"
            )
            last_visible_log_at = now
            continue

        if event_type == "error":
            raise payload

        if event_type == "done":
            break

        event = payload

        if not isinstance(event, dict):
            continue

        received_event = True
        last_event_at = time.monotonic()
        error_detail = event.get("errorDetail") or {}
        error_message = error_detail.get("message") or event.get("error")

        if error_message:
            raise RuntimeError(str(error_message))

        status = sanitize_log_text(event.get("status", "")).strip()
        layer_id = str(event.get("id") or "image")
        progress = sanitize_log_text(event.get("progress", "")).strip()
        now = time.monotonic()
        status_changed = bool(status) and layer_statuses.get(layer_id) != status
        progress_due = bool(progress) and now - last_progress_log >= 5

        if status_changed or progress_due:
            message = f"[docker] {layer_id}: {status}" if status else f"[docker] {layer_id}"

            if progress:
                message += f" {progress}"

            write_log(message)
            last_visible_log_at = now

            if progress:
                last_progress_log = now
        elif now - last_visible_log_at >= DOCKER_PULL_HEARTBEAT_SECONDS:
            elapsed_seconds = int(now - pull_started_at)
            elapsed_minutes, elapsed_remainder = divmod(elapsed_seconds, 60)
            current_status = status or layer_statuses.get(layer_id) or "처리 중"
            write_log(
                f"[docker] 작업 진행 중: {layer_id} / {current_status}. "
                f"전체 경과: {elapsed_minutes}분 {elapsed_remainder}초, "
                "Docker 응답 수신 중"
            )
            last_visible_log_at = now

        if status:
            layer_statuses[layer_id] = status

    pull_thread.join(timeout=1)

    if not received_event:
        write_log("[docker] 이미지 Pull 명령이 종료되었지만 진행 이벤트가 없었습니다.")

    write_log("[docker] 모든 레이어 처리가 끝났습니다. 로컬 이미지 등록 상태를 확인합니다.")
    client.images.get(image)
    write_log("[docker] 공식 Palworld 이미지가 Docker Engine에 정상 등록되었습니다.")


def write_server_control_log(message: str) -> None:
    ensure_data_dirs()
    now = datetime.now().isoformat(timespec="seconds")
    clean_message = sanitize_log_text(message)

    with SERVER_CONTROL_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {clean_message}\n")


def clear_server_control_log() -> None:
    ensure_data_dirs()

    try:
        SERVER_CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass


def default_restart_schedule() -> dict:
    return {
        "enabled": False,
        "restart_time": "04:00",
        "last_run_date": "",
        "last_run_at": "",
        "last_result": "not_run",
        "last_message": "",
    }


def normalize_restart_time(value: str) -> str:
    normalized = str(value or "").strip()

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise ValueError("재시작 시간은 HH:MM 형식으로 입력해주세요.")

    return normalized


def load_restart_schedule() -> dict:
    ensure_data_dirs()

    with RESTART_SCHEDULE_LOCK:
        schedule = default_restart_schedule()

        if RESTART_SCHEDULE_FILE.exists():
            try:
                stored = json.loads(RESTART_SCHEDULE_FILE.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    schedule.update(stored)
            except (OSError, json.JSONDecodeError):
                pass

        try:
            schedule["restart_time"] = normalize_restart_time(schedule.get("restart_time", "04:00"))
        except ValueError:
            schedule["restart_time"] = "04:00"

        schedule["enabled"] = bool(schedule.get("enabled", False))
        return schedule


def persist_restart_schedule(schedule: dict) -> dict:
    ensure_data_dirs()
    normalized = default_restart_schedule()
    normalized.update(schedule)
    normalized["enabled"] = bool(normalized.get("enabled", False))
    normalized["restart_time"] = normalize_restart_time(normalized.get("restart_time", "04:00"))

    with RESTART_SCHEDULE_LOCK:
        temporary_path = RESTART_SCHEDULE_FILE.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(RESTART_SCHEDULE_FILE)

    return normalized


def restart_schedule_response(schedule: dict | None = None) -> dict:
    current = schedule or load_restart_schedule()
    next_run = ""

    if current.get("enabled"):
        hour, minute = (int(part) for part in current["restart_time"].split(":"))
        now = datetime.now(KST)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        is_due_minute = now.strftime("%H:%M") == current["restart_time"]
        has_run_today = current.get("last_run_date") == now.date().isoformat()

        if target <= now and (not is_due_minute or has_run_today):
            target += timedelta(days=1)

        next_run = target.isoformat(timespec="minutes")

    return {
        **current,
        "timezone": "Asia/Seoul",
        "next_run_at": next_run,
    }


def update_restart_schedule_result(result: str, message: str) -> None:
    with RESTART_SCHEDULE_LOCK:
        schedule = load_restart_schedule()
        schedule["last_result"] = result
        schedule["last_message"] = message
        persist_restart_schedule(schedule)


def run_scheduled_restart_if_due() -> None:
    now = datetime.now(KST)

    with RESTART_SCHEDULE_LOCK:
        schedule = load_restart_schedule()

        if not schedule.get("enabled") or now.strftime("%H:%M") != schedule["restart_time"]:
            return

        today = now.date().isoformat()

        if schedule.get("last_run_date") == today:
            return

        schedule["last_run_date"] = today
        schedule["last_run_at"] = now.isoformat(timespec="seconds")
        schedule["last_result"] = "running"
        schedule["last_message"] = "예약 재시작을 처리하는 중입니다."
        persist_restart_schedule(schedule)

    try:
        client = docker.from_env()

        try:
            container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            message = "예약 시간이 되었지만 게임 서버 컨테이너가 없어 재시작을 건너뛰었습니다."
            write_server_control_log(message)
            update_restart_schedule_result("skipped", message)
            return

        container.reload()

        if container.status != "running" or not server_container_uses_official_runtime(container):
            message = "예약 시간이 되었지만 게임 서버가 실행 중이 아니어서 재시작을 건너뛰었습니다."
            write_server_control_log(message)
            update_restart_schedule_result("skipped", message)
            return

        write_server_control_log(
            f"KST {schedule['restart_time']} 예약에 따라 Palworld 게임 서버 컨테이너를 재시작합니다."
        )
        container.restart(timeout=15)
        message = "예약된 Palworld 게임 서버 컨테이너 재시작이 완료되었습니다."
        write_server_control_log(message)
        update_restart_schedule_result("success", message)
    except Exception as error:
        message = f"예약 재시작 중 오류가 발생했습니다: {error}"
        write_server_control_log(message)
        update_restart_schedule_result("error", message)


def restart_scheduler_loop() -> None:
    while not RESTART_SCHEDULER_STOP.wait(5):
        run_scheduled_restart_if_due()


@app.on_event("startup")
def start_restart_scheduler() -> None:
    global RESTART_SCHEDULER_THREAD

    if RESTART_SCHEDULER_THREAD and RESTART_SCHEDULER_THREAD.is_alive():
        return

    RESTART_SCHEDULER_STOP.clear()
    RESTART_SCHEDULER_THREAD = threading.Thread(
        target=restart_scheduler_loop,
        name="palworld-restart-scheduler",
        daemon=True,
    )
    RESTART_SCHEDULER_THREAD.start()


@app.on_event("shutdown")
def stop_restart_scheduler() -> None:
    RESTART_SCHEDULER_STOP.set()

    if RESTART_SCHEDULER_THREAD and RESTART_SCHEDULER_THREAD.is_alive():
        RESTART_SCHEDULER_THREAD.join(timeout=6)


def set_status(status: str) -> None:
    ensure_data_dirs()
    INSTALL_STATUS_FILE.write_text(status, encoding="utf-8")


def get_status() -> str:
    if not INSTALL_STATUS_FILE.exists():
        return "not_started"

    return INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip()


def has_official_runtime_install_marker() -> bool:
    if not INSTALL_REQUEST_FILE.exists():
        return False

    try:
        marker = INSTALL_REQUEST_FILE.read_text(encoding="utf-8")
    except OSError:
        return False

    return (
        "distribution=pocketpair-official-docker" in marker
        and f"runtime_image={PALWORLD_RUNTIME_IMAGE}" in marker
    )


def get_effective_install_status() -> str:
    status = get_status()

    if status == "completed" and not has_official_runtime_install_marker():
        return "update_required"

    return status


def get_config_path() -> Path:
    return DATA_DIR / "server" / "Pal" / "Saved" / "Config" / "LinuxServer" / "PalWorldSettings.ini"


def default_config() -> dict:
    return {
        "ServerName": "TechTim Palworld Server",
        "ServerDescription": "",
        "AdminPassword": "",
        "ServerPassword": "",
        "PublicPort": SERVER_PORT,
        "MaxPlayers": 32,
        "RCONEnabled": False,
        "RCONPort": RCON_PORT,
    }


def normalize_config(config: dict) -> dict:
    merged = default_config()
    merged.update(config)

    merged["ServerName"] = str(merged.get("ServerName") or "TechTim Palworld Server").strip() or "TechTim Palworld Server"
    merged["ServerDescription"] = str(merged.get("ServerDescription") or "")
    merged["AdminPassword"] = str(merged.get("AdminPassword") or "")
    merged["ServerPassword"] = str(merged.get("ServerPassword") or "")
    merged["PublicPort"] = max(1, min(65535, int(merged.get("PublicPort") or SERVER_PORT)))
    merged["MaxPlayers"] = max(1, min(100, int(merged.get("MaxPlayers") or 32)))
    merged["RCONEnabled"] = bool(merged.get("RCONEnabled"))
    merged["RCONPort"] = max(1, min(65535, int(merged.get("RCONPort") or RCON_PORT)))

    return merged


PALWORLD_OPTION_DEFAULTS = {
    "Difficulty": "None",
    "RandomizerType": "None",
    "RandomizerSeed": "",
    "bIsRandomizerPalLevelRandom": False,
    "DayTimeSpeedRate": 1.0,
    "NightTimeSpeedRate": 1.0,
    "ExpRate": 1.0,
    "PalCaptureRate": 1.0,
    "PalSpawnNumRate": 1.0,
    "PalDamageRateAttack": 1.0,
    "PalDamageRateDefense": 1.0,
    "PlayerDamageRateAttack": 1.0,
    "PlayerDamageRateDefense": 1.0,
    "PlayerStomachDecreaceRate": 1.0,
    "PlayerStaminaDecreaceRate": 1.0,
    "PlayerAutoHPRegeneRate": 1.0,
    "PlayerAutoHpRegeneRateInSleep": 1.0,
    "PalStomachDecreaceRate": 1.0,
    "PalStaminaDecreaceRate": 1.0,
    "PalAutoHPRegeneRate": 1.0,
    "PalAutoHpRegeneRateInSleep": 1.0,
    "BuildObjectHpRate": 1.0,
    "BuildObjectDamageRate": 1.0,
    "BuildObjectDeteriorationDamageRate": 1.0,
    "CollectionDropRate": 1.0,
    "CollectionObjectHpRate": 1.0,
    "CollectionObjectRespawnSpeedRate": 1.0,
    "EnemyDropItemRate": 1.0,
    "DeathPenalty": "All",
    "bEnablePlayerToPlayerDamage": False,
    "bEnableFriendlyFire": False,
    "bEnableInvaderEnemy": True,
    "EnablePredatorBossPal": True,
    "bActiveUNKO": False,
    "bEnableAimAssistPad": True,
    "bEnableAimAssistKeyboard": False,
    "DropItemMaxNum": 3000,
    "DropItemMaxNum_UNKO": 100,
    "BaseCampMaxNum": 128,
    "BaseCampMaxNumInGuild": 3,
    "BaseCampWorkerMaxNum": 15,
    "DropItemAliveMaxHours": 1.0,
    "bAutoResetGuildNoOnlinePlayers": False,
    "AutoResetGuildTimeNoOnlinePlayers": 72.0,
    "GuildPlayerMaxNum": 20,
    "PalEggDefaultHatchingTime": 72.0,
    "WorkSpeedRate": 1.0,
    "AutoSaveSpan": 30.0,
    "CrossplayPlatforms": "(Steam,Xbox,PS5,Mac)",
    "LogFormatType": "Text",
    "bIsMultiplay": False,
    "bIsPvP": False,
    "bHardcore": False,
    "bPalLost": False,
    "bCharacterRecreateInHardcore": False,
    "bCanPickupOtherGuildDeathPenaltyDrop": False,
    "bEnableNonLoginPenalty": True,
    "bEnableFastTravel": True,
    "bIsStartLocationSelectByMap": True,
    "bExistPlayerAfterLogout": False,
    "bEnableDefenseOtherGuildPlayer": False,
    "bInvisibleOtherGuildBaseCampAreaFX": False,
    "bBuildAreaLimit": False,
    "ItemWeightRate": 1.0,
    "bShowPlayerList": False,
    "CoopPlayerMaxNum": 4,
    "ServerPlayerMaxNum": 32,
    "ServerName": "TechTim Palworld Server",
    "ServerDescription": "",
    "AdminPassword": "",
    "ServerPassword": "",
    "PublicPort": SERVER_PORT,
    "PublicIP": "",
    "RCONEnabled": False,
    "RCONPort": RCON_PORT,
    "RESTAPIEnabled": False,
    "RESTAPIPort": 8212,
    "bIsUseBackupSaveData": True,
    "Region": "",
    "bUseAuth": True,
    "BanListURL": "https://api.palworldgame.com/api/banlist.txt",
    "SupplyDropSpan": 180,
    "ChatPostLimitPerMinute": 10,
    "MaxBuildingLimitNum": 0,
    "ServerReplicatePawnCullDistance": 15000.0,
    "bAllowGlobalPalboxExport": True,
    "bAllowGlobalPalboxImport": False,
    "EquipmentDurabilityDamageRate": 1.0,
    "ItemContainerForceMarkDirtyInterval": 1.0,
    "ItemCorruptionMultiplier": 1.0,
    "bEnableFastTravelOnlyBaseCamp": False,
    "bAllowClientMod": True,
    "bIsShowJoinLeftMessage": True,
    "DenyTechnologyList": "()",
    "GuildRejoinCooldownMinutes": 0,
    "BlockRespawnTime": 5.0,
    "RespawnPenaltyDurationThreshold": 0.0,
    "RespawnPenaltyTimeScale": 2.0,
    "bDisplayPvPItemNumOnWorldMap_BaseCamp": False,
    "bDisplayPvPItemNumOnWorldMap_Player": False,
    "AdditionalDropItemWhenPlayerKillingInPvPMode": "PlayerDropItem",
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": 1,
    "bAdditionalDropItemWhenPlayerKillingInPvPMode": False,
    "bAllowEnhanceStat_Health": True,
    "bAllowEnhanceStat_Attack": True,
    "bAllowEnhanceStat_Stamina": True,
    "bAllowEnhanceStat_Weight": True,
    "bAllowEnhanceStat_WorkSpeed": True,
}


PALWORLD_OPTION_ORDER = list(PALWORLD_OPTION_DEFAULTS.keys())
PALWORLD_RAW_STRING_OPTIONS = {
    "Difficulty",
    "RandomizerType",
    "DeathPenalty",
    "LogFormatType",
    "AdditionalDropItemWhenPlayerKillingInPvPMode",
}
PALWORLD_ADVANCED_KEYS = {
    "Difficulty",
    "RandomizerType",
    "RandomizerSeed",
    "bIsRandomizerPalLevelRandom",
    "DayTimeSpeedRate",
    "NightTimeSpeedRate",
    "ExpRate",
    "PalCaptureRate",
    "PalSpawnNumRate",
    "PalDamageRateAttack",
    "PalDamageRateDefense",
    "PlayerDamageRateAttack",
    "PlayerDamageRateDefense",
    "PlayerStomachDecreaceRate",
    "PlayerStaminaDecreaceRate",
    "PlayerAutoHPRegeneRate",
    "PlayerAutoHpRegeneRateInSleep",
    "PalStomachDecreaceRate",
    "PalStaminaDecreaceRate",
    "PalAutoHPRegeneRate",
    "PalAutoHpRegeneRateInSleep",
    "BuildObjectHpRate",
    "BuildObjectDamageRate",
    "BuildObjectDeteriorationDamageRate",
    "CollectionDropRate",
    "CollectionObjectHpRate",
    "CollectionObjectRespawnSpeedRate",
    "EnemyDropItemRate",
    "DeathPenalty",
    "bEnablePlayerToPlayerDamage",
    "bEnableFriendlyFire",
    "bEnableInvaderEnemy",
    "bActiveUNKO",
    "EnablePredatorBossPal",
    "DropItemMaxNum",
    "DropItemMaxNum_UNKO",
    "BaseCampMaxNum",
    "BaseCampMaxNumInGuild",
    "BaseCampWorkerMaxNum",
    "DropItemAliveMaxHours",
    "bAutoResetGuildNoOnlinePlayers",
    "AutoResetGuildTimeNoOnlinePlayers",
    "GuildPlayerMaxNum",
    "PalEggDefaultHatchingTime",
    "WorkSpeedRate",
    "AutoSaveSpan",
    "CrossplayPlatforms",
    "bIsMultiplay",
    "bIsPvP",
    "bHardcore",
    "bPalLost",
    "bCharacterRecreateInHardcore",
    "bCanPickupOtherGuildDeathPenaltyDrop",
    "bEnableNonLoginPenalty",
    "bEnableFastTravel",
    "bIsStartLocationSelectByMap",
    "bExistPlayerAfterLogout",
    "bEnableDefenseOtherGuildPlayer",
    "bInvisibleOtherGuildBaseCampAreaFX",
    "bBuildAreaLimit",
    "bEnableAimAssistPad",
    "bEnableAimAssistKeyboard",
    "ItemWeightRate",
    "bShowPlayerList",
    "CoopPlayerMaxNum",
    "PublicIP",
    "RESTAPIEnabled",
    "RESTAPIPort",
    "bIsUseBackupSaveData",
    "Region",
    "bUseAuth",
    "BanListURL",
    "SupplyDropSpan",
    "ChatPostLimitPerMinute",
    "MaxBuildingLimitNum",
    "ServerReplicatePawnCullDistance",
    "bAllowGlobalPalboxExport",
    "bAllowGlobalPalboxImport",
    "EquipmentDurabilityDamageRate",
    "ItemContainerForceMarkDirtyInterval",
    "ItemCorruptionMultiplier",
    "bEnableFastTravelOnlyBaseCamp",
    "bAllowClientMod",
    "bIsShowJoinLeftMessage",
    "LogFormatType",
    "DenyTechnologyList",
    "GuildRejoinCooldownMinutes",
    "BlockRespawnTime",
    "RespawnPenaltyDurationThreshold",
    "RespawnPenaltyTimeScale",
    "bDisplayPvPItemNumOnWorldMap_BaseCamp",
    "bDisplayPvPItemNumOnWorldMap_Player",
    "AdditionalDropItemWhenPlayerKillingInPvPMode",
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode",
    "bAdditionalDropItemWhenPlayerKillingInPvPMode",
    "bAllowEnhanceStat_Health",
    "bAllowEnhanceStat_Attack",
    "bAllowEnhanceStat_Stamina",
    "bAllowEnhanceStat_Weight",
    "bAllowEnhanceStat_WorkSpeed",
}


def split_top_level_options(option_text: str) -> list[str]:
    parts = []
    current = []
    quote = False
    escape = False
    depth = 0

    for char in option_text:
        if escape:
            current.append(char)
            escape = False
            continue

        if char == "\\" and quote:
            current.append(char)
            escape = True
            continue

        if char == '"':
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")" and depth > 0:
            depth -= 1

        if char == "," and not quote and depth == 0:
            part = "".join(current).strip()

            if part:
                parts.append(part)

            current = []
            continue

        current.append(char)

    part = "".join(current).strip()

    if part:
        parts.append(part)

    return parts


def parse_palworld_value(raw_value: str):
    if raw_value.startswith('"') and raw_value.endswith('"'):
        return raw_value[1:-1].replace('\\"', '"').replace("\\\\", "\\")

    if raw_value in {"True", "False"}:
        return raw_value == "True"

    if raw_value.startswith("(") and raw_value.endswith(")"):
        return raw_value

    try:
        return float(raw_value) if "." in raw_value else int(raw_value)
    except ValueError:
        return raw_value


def split_palworld_options(option_text: str) -> dict:
    values = {}

    for part in split_top_level_options(option_text):
        if "=" not in part:
            continue

        key, raw_value = part.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()

        if not key:
            continue

        values[key] = parse_palworld_value(raw_value)

    return values


def normalize_palworld_option(key: str, value):
    default_value = PALWORLD_OPTION_DEFAULTS.get(key)

    if isinstance(default_value, bool):
        if isinstance(value, str):
            return value.lower() == "true"

        return bool(value)

    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return int(value or 0)

    if isinstance(default_value, float):
        return float(value or 0)

    return str(value or "")


def normalize_advanced_options(options: dict) -> dict:
    normalized = {}

    for key, value in (options or {}).items():
        if key not in PALWORLD_ADVANCED_KEYS:
            continue

        normalized[key] = normalize_palworld_option(key, value)

    return normalized


def read_palworld_options() -> dict:
    config_path = get_config_path()

    if not config_path.exists():
        return PALWORLD_OPTION_DEFAULTS.copy()

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return PALWORLD_OPTION_DEFAULTS.copy()

    match = re.search(r"OptionSettings=\((.*)\)", text, re.DOTALL)

    if not match:
        return PALWORLD_OPTION_DEFAULTS.copy()

    merged = PALWORLD_OPTION_DEFAULTS.copy()
    merged.update(split_palworld_options(match.group(1)))
    return merged


def palworld_option_value(key: str, value) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return f"{value:.6f}"

    if isinstance(value, str) and value.startswith("(") and value.endswith(")"):
        return value

    if key in PALWORLD_RAW_STRING_OPTIONS:
        return str(value)

    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_config() -> dict:
    options = read_palworld_options()
    config = normalize_config({
        "ServerName": options.get("ServerName"),
        "ServerDescription": options.get("ServerDescription"),
        "AdminPassword": options.get("AdminPassword"),
        "ServerPassword": options.get("ServerPassword"),
        "PublicPort": options.get("PublicPort"),
        "MaxPlayers": options.get("ServerPlayerMaxNum"),
        "RCONEnabled": options.get("RCONEnabled"),
        "RCONPort": options.get("RCONPort"),
    })
    config["AdvancedOptions"] = {
        key: options.get(key, PALWORLD_OPTION_DEFAULTS.get(key))
        for key in PALWORLD_OPTION_ORDER
        if key in PALWORLD_ADVANCED_KEYS
    }

    return config


def write_config(config: dict) -> Path:
    config_path = get_config_path()
    normalized = normalize_config(config)
    options = read_palworld_options()

    options.update({
        "ServerName": normalized["ServerName"],
        "ServerDescription": normalized["ServerDescription"],
        "AdminPassword": normalized["AdminPassword"],
        "ServerPassword": normalized["ServerPassword"],
        "PublicPort": normalized["PublicPort"],
        "ServerPlayerMaxNum": normalized["MaxPlayers"],
        "RCONEnabled": normalized["RCONEnabled"],
        "RCONPort": normalized["RCONPort"],
    })
    options.update(normalize_advanced_options(config.get("AdvancedOptions") or {}))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    ordered_keys = PALWORLD_OPTION_ORDER + [key for key in options if key not in PALWORLD_OPTION_ORDER]
    option_text = ",".join(f"{key}={palworld_option_value(key, options[key])}" for key in ordered_keys)

    config_path.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        f"OptionSettings=({option_text})\n",
        encoding="utf-8",
    )

    return config_path


def create_default_config() -> Path:
    config_path = get_config_path()

    if config_path.exists():
        return config_path

    return write_config(default_config())


def list_saved_world_names() -> list[str]:
    if not SAVED_WORLDS_DIR.exists():
        return []

    worlds = set()

    for path in SAVED_WORLDS_DIR.iterdir():
        if path.name.startswith("."):
            continue

        if path.is_dir():
            worlds.add(path.name)

    return sorted(worlds, key=str.casefold)


def list_world_options(config: dict | None = None) -> list[str]:
    return list_saved_world_names()


def validate_world_start_config(config: dict) -> str | None:
    if not str(config.get("ServerName") or "").strip():
        return "서버 이름이 비어 있습니다. 서버 설정에서 서버 이름을 입력해주세요."

    return None


def server_container_keeps_stdin_open(container) -> bool:
    config = container.attrs.get("Config", {})
    return bool(config.get("OpenStdin"))


def server_container_uses_official_runtime(container) -> bool:
    config = container.attrs.get("Config", {})
    entrypoint = config.get("Entrypoint") or []

    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]

    return (
        config.get("Image") == PALWORLD_RUNTIME_IMAGE
        and "/pal/helper.sh" in entrypoint
    )


def is_server_container_running() -> bool:
    try:
        client = docker.from_env()
        container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        container.reload()
        return container.status == "running" and server_container_uses_official_runtime(container)
    except docker.errors.NotFound:
        return False
    except Exception:
        return False


def read_server_control_log() -> str:
    if not SERVER_CONTROL_LOG_FILE.exists():
        return ""

    try:
        return sanitize_log_text(SERVER_CONTROL_LOG_FILE.read_text(encoding="utf-8"))
    except OSError:
        return ""


def read_container_log(container, tail: int = 200) -> str:
    return sanitize_log_text(container.logs(
        stdout=True,
        stderr=True,
        tail=tail,
    ).decode("utf-8", errors="replace"))


def combined_server_log(container=None, include_control_log: bool = True, tail: int = 200) -> str:
    logs = ""

    if container is not None:
        try:
            logs = read_container_log(container, tail=tail)
        except Exception as e:
            logs = f"서버 로그 조회 실패: {e}"

    control_log = read_server_control_log()

    if include_control_log and control_log:
        return (logs.rstrip() + "\n\n[패널 제어 로그]\n" + control_log).strip()

    return logs


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = (target_dir / member).resolve()

            if member_path != target_root and not str(member_path).startswith(str(target_root) + os.sep):
                raise ValueError("ZIP 파일에 허용되지 않는 경로가 포함되어 있습니다.")

        zip_ref.extractall(target_dir)


def saved_root() -> Path:
    ensure_data_dirs()
    return SAVED_ROOT_DIR.resolve()


def resolve_saved_path(relative_path: str = "") -> Path:
    root = saved_root()
    cleaned = (relative_path or "").strip().replace("\\", "/").lstrip("/")
    candidate = (root / cleaned).resolve()

    if candidate != root and not str(candidate).startswith(str(root) + os.sep):
        raise HTTPException(status_code=400, detail="Saved 폴더 밖으로 이동할 수 없습니다.")

    return candidate


def saved_relative_path(path: Path) -> str:
    root = saved_root()
    resolved = path.resolve()

    if resolved == root:
        return ""

    return resolved.relative_to(root).as_posix()


def validate_entry_name(name: str) -> str:
    cleaned = Path(name or "").name.strip()

    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="올바른 이름을 입력해주세요.")

    return cleaned


def file_entry(path: Path) -> dict:
    stat = path.stat()

    return {
        "name": path.name,
        "path": saved_relative_path(path),
        "type": "dir" if path.is_dir() else "file",
        "size": stat.st_size if path.is_file() else 0,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def require_server_stopped_for_file_write() -> None:
    if is_server_container_running():
        raise HTTPException(
            status_code=409,
            detail="서버 실행 중에는 파일 업로드, 삭제, 폴더 생성을 할 수 없습니다. 서버를 중지한 뒤 다시 시도해주세요.",
        )


def install_palworld_job() -> None:
    global INSTALL_JOB_ACTIVE

    with INSTALL_JOB_LOCK:
        INSTALL_JOB_ACTIVE = True

    ensure_data_dirs()

    INSTALL_LOG_FILE.write_text("", encoding="utf-8")
    set_status("running")

    write_log("Palworld Dedicated Server 설치 작업을 시작합니다.")
    write_log("Pocketpair 공식 Palworld 1.0 Docker 이미지를 사용합니다.")
    write_log(f"공식 런타임 이미지: {PALWORLD_RUNTIME_IMAGE}")

    try:
        client = docker.from_env()
        write_log("Docker Engine 연결 성공.")

        max_attempts = 3
        image_ready = False

        for attempt in range(1, max_attempts + 1):
            write_log(f"공식 이미지 다운로드 시도 {attempt}/{max_attempts}")
            write_log("최초 설치는 공식 서버 이미지 용량에 따라 수 분 이상 걸릴 수 있습니다.")

            try:
                pull_docker_image_with_progress(client, PALWORLD_RUNTIME_IMAGE)
                image_ready = True
                write_log(f"공식 이미지 다운로드 시도 {attempt}/{max_attempts} 성공")
                break
            except Exception as attempt_error:
                write_log(f"WARNING: 공식 이미지 다운로드 시도 {attempt}/{max_attempts} 실패: {attempt_error}")

            if attempt < max_attempts:
                time.sleep(20)

        if not image_ready:
            write_log(f"ERROR: 공식 Palworld 이미지를 {max_attempts}회 모두 다운로드하지 못했습니다.")
            write_log("GHCR 연결 상태와 Docker Engine 로그를 확인해주세요.")
            set_status("failed")
            return

        write_log("공식 이미지 검증이 완료되었습니다.")
        write_log("공식 런타임 helper 스크립트를 준비합니다.")
        ensure_official_runtime_files()
        write_log(f"런타임 helper 준비 완료: {RUNTIME_HELPER_FILE}")

        write_log(f"세이브 및 설정 디렉토리를 확인합니다: {SAVED_ROOT_DIR}")
        config_existed = get_config_path().exists()
        create_default_config()

        if config_existed:
            write_log("기존 PalWorldSettings.ini를 유지합니다.")
        else:
            write_log(f"기본 PalWorldSettings.ini를 생성했습니다: {get_config_path()}")

        write_log("설치 완료 정보를 기록합니다.")

        INSTALL_REQUEST_FILE.write_text(
            "TechTim Palworld Dedicated Server install completed.\n"
            f"game={GAME_CODE}\n"
            f"panel_version={PANEL_VERSION}\n"
            "distribution=pocketpair-official-docker\n"
            f"runtime_image={PALWORLD_RUNTIME_IMAGE}\n"
            f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )

        write_log("Pocketpair 공식 Palworld 1.0 서버 이미지 설치가 완료되었습니다.")
        write_log(f"세이브 및 설정 경로: {SAVED_ROOT_DIR}")
        write_log("이제 Web GUI에서 PalWorldSettings.ini를 저장하고 서버를 시작할 수 있습니다.")
        set_status("completed")

    except Exception as e:
        write_log(f"ERROR: 설치 작업 중 예외 발생: {e}")
        set_status("failed")
    finally:
        with INSTALL_JOB_LOCK:
            INSTALL_JOB_ACTIVE = False


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    init_auth_if_needed()

    if get_current_user(request):
        if must_change_password():
            return RedirectResponse(url="/change-password", status_code=302)

        return RedirectResponse(url="/", status_code=302)

    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>TechTim Palworld Login</title>
  <style>
    body { min-height: 100vh; margin: 0; font-family: Arial, sans-serif; background: linear-gradient(135deg, rgba(9, 22, 29, 0.42), rgba(20, 53, 42, 0.28)), url("/static/palworld-panel-bg.png") center / cover fixed no-repeat; color: #1f2937; }
    .box { max-width: 420px; margin: 100px auto; background: rgba(255, 255, 255, 0.92); border: 1px solid rgba(255,255,255,0.56); border-radius: 16px; padding: 34px; box-shadow: 0 24px 70px rgba(0,0,0,0.34); backdrop-filter: blur(12px); }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { color: #6b7280; line-height: 1.5; }
    label { display: block; margin-top: 18px; font-size: 14px; font-weight: bold; }
    input { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 13px; border: 1px solid #d1d5db; border-radius: 10px; font-size: 15px; }
    button { width: 100%; margin-top: 24px; border: 0; border-radius: 10px; padding: 14px; font-weight: bold; cursor: pointer; background: #2563eb; color: white; font-size: 15px; }
    .hint { margin-top: 18px; padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; color: #374151; font-size: 13px; }
    .error { margin-top: 14px; color: #dc2626; white-space: pre-line; font-size: 14px; }
  </style>
</head>
<body>
  <div class="box">
    <h1>TechTim Palworld Panel</h1>
    <p>관리자 계정으로 로그인하세요.</p>

    <form id="loginForm">
      <label>아이디</label>
      <input id="username" type="text" value="admin" autocomplete="username">

      <label>비밀번호</label>
      <input id="password" type="password" placeholder="비밀번호" autocomplete="current-password">

      <button type="submit">로그인</button>
    </form>

    <div class="hint">
      최초 기본 계정은 <b>admin / admin</b> 입니다.<br>
      첫 로그인 후 반드시 비밀번호를 변경해야 합니다.
    </div>

    <div id="error" class="error"></div>
  </div>

  <script>
    document.getElementById("loginForm").addEventListener("submit", function (event) {
      event.preventDefault();
      login();
    });

    async function login() {
      const errorBox = document.getElementById("error");
      errorBox.innerText = "";

      const username = document.getElementById("username").value.trim();
      const password = document.getElementById("password").value;

      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password })
        });

        const data = await response.json();

        if (!response.ok) {
          errorBox.innerText = data.detail || "로그인 실패";
          return;
        }

        window.location.href = data.must_change_password ? "/change-password" : "/";
      } catch (err) {
        errorBox.innerText = "로그인 요청 실패: " + err;
      }
    }
  </script>
</body>
</html>
"""


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)

    if not must_change_password():
        return RedirectResponse(url="/", status_code=302)

    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>Change Admin Password</title>
  <style>
    body { min-height: 100vh; margin: 0; font-family: Arial, sans-serif; background: linear-gradient(135deg, rgba(9, 22, 29, 0.42), rgba(20, 53, 42, 0.28)), url("/static/palworld-panel-bg.png") center / cover fixed no-repeat; color: #1f2937; }
    .box { max-width: 460px; margin: 90px auto; background: rgba(255, 255, 255, 0.92); border: 1px solid rgba(255,255,255,0.56); border-radius: 16px; padding: 34px; box-shadow: 0 24px 70px rgba(0,0,0,0.34); backdrop-filter: blur(12px); }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { color: #6b7280; line-height: 1.5; }
    label { display: block; margin-top: 18px; font-size: 14px; font-weight: bold; }
    input { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 13px; border: 1px solid #d1d5db; border-radius: 10px; font-size: 15px; }
    button { width: 100%; margin-top: 24px; border: 0; border-radius: 10px; padding: 14px; font-weight: bold; cursor: pointer; background: #2563eb; color: white; font-size: 15px; }
    .error { margin-top: 14px; color: #dc2626; white-space: pre-line; font-size: 14px; }
  </style>
</head>
<body>
  <div class="box">
    <h1>관리자 비밀번호 변경</h1>
    <p>처음 사용할 새 관리자 비밀번호를 입력해주세요.</p>

    <form id="changePasswordForm">
      <label>새 비밀번호</label>
      <input id="newPassword" type="password" autocomplete="new-password">

      <label>새 비밀번호 확인</label>
      <input id="confirmPassword" type="password" autocomplete="new-password">

      <button type="submit">비밀번호 변경</button>
    </form>

    <div id="error" class="error"></div>
  </div>

  <script>
    document.getElementById("changePasswordForm").addEventListener("submit", function (event) {
      event.preventDefault();
      changePassword();
    });

    async function changePassword() {
      const errorBox = document.getElementById("error");
      errorBox.innerText = "";

      const newPassword = document.getElementById("newPassword").value;
      const confirmPassword = document.getElementById("confirmPassword").value;

      if (!newPassword || !confirmPassword) {
        errorBox.innerText = "모든 항목을 입력해주세요.";
        return;
      }

      if (newPassword !== confirmPassword) {
        errorBox.innerText = "새 비밀번호와 확인 값이 일치하지 않습니다.";
        return;
      }

      if (newPassword.length < 4) {
        errorBox.innerText = "새 비밀번호는 최소 4자 이상으로 입력해주세요.";
        return;
      }

      try {
        const response = await fetch("/api/auth/change-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            new_password: newPassword
          })
        });

        const data = await response.json();

        if (!response.ok) {
          errorBox.innerText = data.detail || "비밀번호 변경 실패";
          return;
        }

        window.location.href = "/";
      } catch (err) {
        errorBox.innerText = "비밀번호 변경 요청 실패: " + err;
      }
    }
  </script>
</body>
</html>
"""


@app.post("/api/auth/login")
def api_login(payload: LoginRequest, response: Response):
    auth_data = load_auth()

    if payload.username != auth_data.get("username"):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    if not verify_password(payload.password, auth_data):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    token = create_session(payload.username)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )

    return {
        "status": "ok",
        "must_change_password": bool(auth_data.get("must_change_password", True)),
    }


@app.post("/api/auth/change-password")
def api_change_password(payload: ChangePasswordRequest, request: Request):
    require_auth(request)

    auth_data = load_auth()

    if not auth_data.get("must_change_password", True):
        raise HTTPException(status_code=400, detail="이미 비밀번호가 변경되었습니다.")

    if len(payload.new_password) < 4:
        raise HTTPException(status_code=400, detail="새 비밀번호는 최소 4자 이상이어야 합니다.")

    salt = secrets.token_hex(16)
    auth_data["salt"] = salt
    auth_data["password_hash"] = password_hash(payload.new_password, salt)
    auth_data["must_change_password"] = False

    save_auth(auth_data)

    return {
        "status": "ok",
        "message": "비밀번호가 변경되었습니다.",
    }


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME)

    return {
        "status": "ok",
        "message": "로그아웃되었습니다.",
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)

    if must_change_password():
        return RedirectResponse(url="/change-password", status_code=302)

    html = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>TechTim Palworld Server Panel</title>
  <style>
    body { min-height: 100vh; margin: 0; font-family: Arial, sans-serif; background: linear-gradient(135deg, rgba(9, 22, 29, 0.42), rgba(20, 53, 42, 0.28)), url("/static/palworld-panel-bg.png") center / cover fixed no-repeat; color: #1f2937; }
    .wrap { max-width: 1180px; margin: 40px auto; background: rgba(255, 255, 255, 0.65); border: 1px solid rgba(255,255,255,0.58); border-radius: 16px; padding: 40px; box-shadow: 0 24px 80px rgba(0,0,0,0.35); backdrop-filter: blur(12px); }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
    h1 { margin: 0; font-size: 34px; }
    .top-links { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .top-link { display: inline-flex; align-items: center; justify-content: center; width: 38px; height: 38px; padding: 0; border: 1px solid #d1d5db; border-radius: 999px; color: #4b5563; background: #f9fafb; text-decoration: none; appearance: none; line-height: 1; }
    .top-link:hover { background: #f3f4f6; border-color: #9ca3af; }
    .top-link img { display: block; width: 20px; height: 20px; opacity: 0.86; }
    .top-link svg { display: block; width: 21px; height: 21px; }
    .top-logout { color: #374151; }
    .top-logout:hover { color: #111827; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 24px; }
    .card { display: flex; align-items: center; gap: 14px; min-height: 72px; background: rgba(249, 250, 251, 0.88); border: 1px solid rgba(229, 231, 235, 0.88); border-radius: 12px; padding: 16px; }
    .card-icon { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 46px; width: 46px; height: 46px; border-radius: 10px; overflow: hidden; color: #ffffff; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.18), 0 8px 18px rgba(17, 24, 39, 0.12); }
    .card-icon svg { display: block; width: 26px; height: 26px; }
    .card-icon img { display: block; width: 100%; height: 100%; }
    .card-icon.palworld-mark { background: #1f8eb8; }
    .card-icon.palworld-mark img { object-fit: cover; }
    .card-icon.status-ok { background: #16a34a; }
    .card-icon.status-bad { background: #dc2626; }
    .card-icon.status-pending { background: linear-gradient(135deg, #d97706, #92400e); }
    .card-icon.server-live { background: linear-gradient(135deg, #0f766e, #155e75); }
    .card-icon.version-mark { background: linear-gradient(135deg, #374151, #111827); }
    .card-text { min-width: 0; }
    .label { font-size: 13px; color: #6b7280; margin-bottom: 8px; }
    .value { font-size: 20px; font-weight: bold; }
    .actions { margin-top: 24px; display: grid; grid-template-columns: repeat(auto-fit, minmax(136px, 1fr)); gap: 12px; align-items: stretch; }
    .actions button { width: 100%; min-height: 48px; }
    .config { margin-top: 24px; padding: 20px; border: 1px solid rgba(229, 231, 235, 0.88); border-radius: 12px; background: rgba(255, 255, 255, 0.9); transition: background 0.2s ease, opacity 0.2s ease; }
    .config-body { transition: filter 0.2s ease, opacity 0.2s ease; }
    .config.locked { background: rgba(255, 255, 255, 0.66); }
    .config.locked .config-body { filter: blur(1.4px); opacity: 0.58; pointer-events: none; user-select: none; }
    .config.locked .settings-hub-pane:first-child { filter: blur(1.2px); opacity: 0.58; pointer-events: none; user-select: none; }
    .config h2 { margin: 0 0 16px; font-size: 22px; }
    .config-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    .advanced-card { position: relative; margin-top: 18px; min-height: 152px; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.66); background: linear-gradient(90deg, rgba(8, 38, 48, 0.92), rgba(20, 81, 71, 0.48)), url("/static/palworld-settings-bg.png") center / cover no-repeat; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); color: #ffffff; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.12), 0 16px 34px rgba(7, 18, 26, 0.16); }
    .advanced-card::before { content: ""; position: absolute; inset: 8px; border-radius: 10px; border: 1px solid rgba(125, 211, 252, 0.44); background: linear-gradient(90deg, rgba(34,211,238,0.22), transparent 26%, transparent 76%, rgba(74,222,128,0.28)); pointer-events: none; }
    .settings-hub-pane { position: relative; z-index: 1; min-width: 0; display: grid; grid-template-columns: 96px minmax(0, 1fr); align-items: center; gap: 20px; padding: 26px 28px; }
    .settings-hub-pane + .settings-hub-pane { border-left: 1px solid rgba(255,255,255,0.28); background: linear-gradient(90deg, rgba(5,44,47,0.18), rgba(13,71,64,0.32)); }
    .advanced-copy { position: relative; z-index: 1; min-width: 0; padding-left: 22px; }
    .advanced-copy::before { content: ""; position: absolute; left: 0; top: 4px; bottom: 4px; width: 4px; border-radius: 999px; background: linear-gradient(180deg, #67e8f9, #a7f3d0 52%, #fde68a); box-shadow: 0 0 18px rgba(103,232,249,0.58); }
    .advanced-title { font-size: 20px; font-weight: bold; margin-bottom: 6px; }
    .advanced-subtitle { color: rgba(255,255,255,0.82); font-size: 13px; line-height: 1.45; }
    .advanced-button { position: relative; z-index: 1; display: inline-flex; flex-direction: column; align-items: center; justify-content: center; gap: 7px; width: 96px; height: 96px; border-radius: 20px; padding: 10px; background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(226,252,247,0.94)); color: #0f766e; border: 1px solid rgba(255,255,255,0.92); box-shadow: 0 18px 38px rgba(0,0,0,0.3), inset 0 0 0 1px rgba(15,118,110,0.2); }
    .advanced-button:hover { transform: translateY(-1px); box-shadow: 0 20px 42px rgba(0,0,0,0.34), inset 0 0 0 1px rgba(15,118,110,0.28); }
    .advanced-button img { width: 46px; height: 46px; display: block; object-fit: cover; border-radius: 12px; }
    .advanced-button svg { width: 44px; height: 44px; display: block; padding: 8px; border-radius: 12px; background: linear-gradient(145deg, #0f766e, #155e75); color: #ffffff; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.24); }
    .advanced-button-label { color: #0f3f3d; font-size: 12px; font-weight: bold; line-height: 1; }
    .restart-hub-summary { display: block; margin-top: 7px; color: #a7f3d0; font-size: 12px; font-weight: bold; }
    .modal-backdrop { position: fixed; inset: 0; z-index: 100; display: none; align-items: center; justify-content: center; padding: 24px; background: rgba(10, 18, 28, 0.62); }
    .modal-backdrop.show { display: flex; }
    .modal { width: min(1060px, 100%); max-height: min(86vh, 900px); overflow: hidden; border-radius: 16px; border: 1px solid rgba(255,255,255,0.45); background: rgba(255,255,255,0.96); box-shadow: 0 28px 90px rgba(0,0,0,0.45); display: flex; flex-direction: column; }
    .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 20px; background: linear-gradient(90deg, rgba(34, 148, 177, 0.18), rgba(73, 182, 122, 0.16)); border-bottom: 1px solid #e5e7eb; }
    .modal-head h2 { margin: 0; font-size: 22px; }
    .modal-close { width: 40px; height: 40px; border-radius: 50%; padding: 0; background: #111827; color: #fff; }
    .modal-body { overflow: auto; padding: 20px; }
    .restart-modal { width: min(620px, 100%); }
    .restart-modal-body { display: grid; gap: 18px; padding: 24px; background: linear-gradient(145deg, rgba(240,253,250,0.98), rgba(239,246,255,0.98)); }
    .restart-modal-intro { display: grid; grid-template-columns: 54px minmax(0, 1fr); align-items: center; gap: 14px; padding: 16px; border: 1px solid #bae6d3; border-radius: 12px; background: rgba(255,255,255,0.82); }
    .restart-modal-intro-icon { display: grid; place-items: center; width: 54px; height: 54px; border-radius: 10px; background: #0f766e; color: #ffffff; }
    .restart-modal-intro-icon svg { width: 30px; height: 30px; }
    .restart-modal-intro strong { display: block; margin-bottom: 4px; color: #134e4a; }
    .restart-modal-intro p { margin: 0; color: #64748b; font-size: 13px; line-height: 1.45; }
    .restart-modal-controls { display: grid; grid-template-columns: minmax(0, 1fr) minmax(180px, 1fr); gap: 14px; }
    .restart-modal-field { min-height: 84px; padding: 14px; border: 1px solid #d1d5db; border-radius: 12px; background: #ffffff; }
    .restart-toggle { display: flex; align-items: center; gap: 9px; height: 100%; margin: 0; font-size: 15px; }
    .restart-toggle input { width: 18px; height: 18px; margin: 0; }
    .restart-time-field input { margin-top: 8px; }
    .restart-schedule-status { min-height: 20px; padding: 12px 14px; border-radius: 10px; background: #e6f4ef; color: #315f55; font-size: 12px; line-height: 1.5; }
    .restart-save-wrap { position: relative; display: inline-flex; }
    .explorer-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
    .explorer-path { min-width: 220px; padding: 10px 12px; border-radius: 10px; background: #eef2f7; color: #1f2937; font-family: Consolas, Monaco, monospace; font-size: 13px; }
    .explorer-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .explorer-actions button { padding: 10px 12px; border-radius: 9px; }
    .explorer-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .explorer-table th, .explorer-table td { padding: 10px 8px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: middle; }
    .explorer-table th { color: #4b5563; font-size: 12px; background: #f9fafb; }
    .explorer-name { display: inline-flex; align-items: center; gap: 8px; min-width: 0; border: 0; padding: 0; background: transparent; color: #0f766e; font-weight: bold; cursor: pointer; }
    .explorer-name.file { color: #1f2937; cursor: default; }
    .explorer-row-actions { display: flex; gap: 6px; justify-content: flex-end; flex-wrap: wrap; }
    .explorer-row-actions button { padding: 8px 10px; border-radius: 8px; font-size: 12px; }
    .explorer-note { margin-top: 12px; color: #6b7280; font-size: 12px; line-height: 1.45; }
    .advanced-group { margin-bottom: 22px; }
    .advanced-group h3 { margin: 0 0 12px; font-size: 17px; color: #111827; }
    .advanced-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .advanced-check { margin-top: 0; align-self: end; min-height: 42px; }
    .range-field { padding: 12px; border: 1px solid #e5e7eb; border-radius: 12px; background: #ffffff; }
    .range-row { display: grid; grid-template-columns: minmax(0, 1fr) 86px; gap: 10px; align-items: center; margin-top: 8px; }
    .range-number { margin-top: 0; text-align: right; }
    input[type="range"].range-slider { appearance: none; width: 100%; height: 10px; margin: 0; padding: 0; border: 0; border-radius: 999px; background: linear-gradient(90deg, #14b8a6 var(--range-fill, 50%), #e5e7eb var(--range-fill, 50%)); cursor: pointer; }
    input[type="range"].range-slider::-webkit-slider-thumb { appearance: none; width: 20px; height: 20px; border: 3px solid #ffffff; border-radius: 50%; background: #0f766e; box-shadow: 0 4px 12px rgba(15,118,110,0.34); }
    input[type="range"].range-slider::-moz-range-thumb { width: 16px; height: 16px; border: 3px solid #ffffff; border-radius: 50%; background: #0f766e; box-shadow: 0 4px 12px rgba(15,118,110,0.34); }
    .modal-foot { display: flex; justify-content: flex-end; gap: 10px; padding: 16px 20px; border-top: 1px solid #e5e7eb; background: #f9fafb; }
    label { display: block; font-size: 13px; font-weight: bold; color: #374151; }
    .field-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .help { position: relative; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; width: 18px; height: 18px; border-radius: 50%; border: 1px solid #9ca3af; color: #4b5563; background: #f9fafb; font-size: 12px; line-height: 1; cursor: help; }
    .help::after { content: attr(data-tip); position: absolute; right: 0; bottom: calc(100% + 8px); z-index: 20; width: 220px; padding: 10px 12px; border-radius: 8px; background: #111827; color: #f9fafb; font-size: 12px; font-weight: normal; line-height: 1.45; box-shadow: 0 10px 24px rgba(0,0,0,0.18); opacity: 0; pointer-events: none; transform: translateY(4px); transition: opacity 0.15s ease, transform 0.15s ease; }
    .help::before { content: ""; position: absolute; right: 7px; bottom: calc(100% + 2px); z-index: 21; border-width: 6px 6px 0 6px; border-style: solid; border-color: #111827 transparent transparent transparent; opacity: 0; pointer-events: none; transition: opacity 0.15s ease; }
    .help:hover::after, .help:focus::after, .help:hover::before, .help:focus::before { opacity: 1; transform: translateY(0); }
    input, select { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 11px; border: 1px solid #d1d5db; border-radius: 10px; font-size: 14px; }
    .checkline { display: inline-flex; align-items: center; gap: 8px; width: fit-content; margin-top: 28px; }
    .checkline input { width: auto; margin: 0; }
    .check-text { display: inline-flex; align-items: center; width: auto; margin: 0; line-height: 1.35; cursor: pointer; }
    button { border: 0; border-radius: 10px; padding: 14px 20px; font-weight: bold; cursor: pointer; background: #2563eb; color: white; }
    button.secondary { background: #e5e7eb; color: #1f2937; }
    button.danger { background: #dc2626; color: white; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .config-save-wrap { position: relative; display: inline-flex; align-items: center; width: fit-content; }
    .config-save-wrap button { position: relative; z-index: 1; }
    .advanced-save-wrap { position: relative; display: inline-flex; align-items: center; width: fit-content; }
    .advanced-save-wrap button { position: relative; z-index: 1; }
    .save-bubble { position: absolute; left: 50%; bottom: calc(100% + 10px); z-index: 30; min-width: 148px; width: max-content; max-width: min(220px, calc(100vw - 48px)); padding: 10px 12px; border-radius: 10px; background: #111827; color: #ffffff; font-size: 13px; font-weight: bold; line-height: 1.35; text-align: center; white-space: nowrap; box-shadow: 0 12px 26px rgba(17,24,39,0.26); opacity: 0; pointer-events: none; transform: translate(-50%, 6px); transition: opacity 0.18s ease, transform 0.18s ease; }
    .save-bubble::after { content: ""; position: absolute; left: 50%; top: 100%; border-width: 7px 7px 0 7px; border-style: solid; border-color: #111827 transparent transparent transparent; transform: translateX(-50%); }
    .save-bubble.show { opacity: 1; transform: translate(-50%, 0); }
    .result { margin-top: 24px; padding: 16px; border-radius: 12px; background: rgba(249, 250, 251, 0.9); border: 1px solid rgba(229, 231, 235, 0.88); color: #374151; min-height: 22px; white-space: pre-line; }
    .log { margin-top: 24px; background: #111827; color: #d1d5db; border-radius: 12px; padding: 18px; min-height: 192px; max-height: 300px; overflow: auto; font-family: Consolas, Monaco, monospace; font-size: 13px; white-space: pre-wrap; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.04); }
    @media (max-width: 900px) {
      .wrap { margin: 0; border-radius: 0; padding: 20px; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .top-links { justify-content: flex-start; }
      .grid, .config-grid { grid-template-columns: 1fr; }
      .advanced-card { grid-template-columns: 1fr; }
      .settings-hub-pane { grid-template-columns: 86px minmax(0, 1fr); gap: 16px; padding: 22px 20px; }
      .settings-hub-pane + .settings-hub-pane { border-left: 0; border-top: 1px solid rgba(255,255,255,0.28); }
      .advanced-grid { grid-template-columns: 1fr; }
      button { width: 100%; }
      .advanced-button { width: 86px; height: 86px; }
      .modal-close { width: 44px; height: 44px; }
      .config-save-wrap { width: 100%; }
      .save-bubble { max-width: calc(100% - 24px); white-space: normal; }
      .help::after { right: auto; left: 50%; transform: translate(-50%, 4px); max-width: min(220px, calc(100vw - 48px)); }
      .help:hover::after, .help:focus::after { transform: translate(-50%, 0); }
      .restart-modal-controls { grid-template-columns: 1fr; }
      .restart-modal-intro { grid-template-columns: 46px minmax(0, 1fr); }
      .restart-modal-intro-icon { width: 46px; height: 46px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <h1>TechTim Palworld Server Panel</h1>
      <div class="top-links">
        <a class="top-link" href="https://discord.gg/Awy6Uh38KW" target="_blank" rel="noopener noreferrer" title="디스코드 접속" aria-label="디스코드 접속">
          <img src="https://cdn.simpleicons.org/discord/5865F2" alt="">
        </a>
        <a class="top-link" href="https://www.youtube.com/@kortechtim" target="_blank" rel="noopener noreferrer" title="유튜브채널 접속" aria-label="유튜브채널 접속">
          <img src="https://cdn.simpleicons.org/youtube/FF0000" alt="">
        </a>
        <button class="top-link top-logout" type="button" onclick="logout()" title="로그아웃" aria-label="로그아웃">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 21V5a2 2 0 0 1 2-2h7" />
            <path d="M8 21h6" />
            <circle cx="11" cy="7.2" r="1.5" />
            <path d="M10.5 9.4 9 12.2l2.4 1.4" />
            <path d="M11.7 11.4 14 12.8" />
            <path d="M9.1 12.6 7.7 15.5" />
            <path d="M11.5 13.8 10.8 17" />
            <path d="M16 12h5" />
            <path d="m19 9 3 3-3 3" />
          </svg>
        </button>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-icon palworld-mark" aria-hidden="true">
          <img src="/static/palworld-card-icon.png" alt="">
        </div>
        <div class="card-text">
          <div class="label">게임</div>
          <div class="value">Palworld</div>
        </div>
      </div>
      <div class="card">
        <div id="installStatusIcon" class="card-icon status-pending" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 6v6l4 2" />
            <circle cx="12" cy="12" r="8" />
          </svg>
        </div>
        <div class="card-text">
          <div class="label">설치 상태</div>
          <div id="installStatus" class="value">확인 중</div>
        </div>
      </div>
      <div class="card">
        <div id="serverStatusIcon" class="card-icon status-pending" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 6v6l4 2" />
            <circle cx="12" cy="12" r="8" />
          </svg>
        </div>
        <div class="card-text">
          <div class="label">서버 상태</div>
          <div id="serverStatus" class="value">확인 중</div>
        </div>
      </div>
      <div class="card">
        <div class="card-icon version-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M7 7h10" />
            <path d="M7 12h6" />
            <path d="M7 17h4" />
            <path d="M17 14v6" />
            <path d="m14.8 17.8 2.2 2.2 2.2-2.2" />
            <rect x="4" y="3" width="16" height="18" rx="2" />
          </svg>
        </div>
        <div class="card-text">
          <div class="label">패널 버전</div>
          <div class="value">__PANEL_VERSION__</div>
        </div>
      </div>
    </div>

    <div id="installLog" class="log">설치 로그가 여기에 표시됩니다.</div>

    <div id="configSection" class="config">
      <div class="config-body">
        <h2>서버 설정</h2>
        <div class="config-grid">
          <label>
            <span class="field-title">서버 이름 <span class="help" tabindex="0" data-tip="Palworld 서버 목록과 접속 화면에 표시되는 이름입니다.">?</span></span>
            <input id="cfgServerName" type="text">
          </label>
          <label>
            <span class="field-title">서버 설명 <span class="help" tabindex="0" data-tip="서버 소개 문구입니다. 비워두어도 서버 실행에는 문제가 없습니다.">?</span></span>
            <input id="cfgDescription" type="text">
          </label>
          <label>
            <span class="field-title">관리자 비밀번호 <span class="help" tabindex="0" data-tip="Palworld 관리자 명령어 권한에 사용할 비밀번호입니다. 운영 서버에서는 반드시 설정하세요.">?</span></span>
            <input id="cfgAdminPassword" type="text">
          </label>
          <label>
            <span class="field-title">서버 비밀번호 <span class="help" tabindex="0" data-tip="비워두면 비밀번호 없이 접속할 수 있습니다. 지인 서버라면 입력하는 것을 권장합니다.">?</span></span>
            <input id="cfgPassword" type="text">
          </label>
          <label>
            <span class="field-title">서버 포트 <span class="help" tabindex="0" data-tip="Palworld 게임 접속용 UDP 포트입니다. 기본값 8211을 권장합니다.">?</span></span>
            <input id="cfgPort" type="number" min="1" max="65535" step="1">
          </label>
          <label>
            <span class="field-title">최대 인원 <span class="help" tabindex="0" data-tip="동시에 접속할 수 있는 최대 플레이어 수입니다. 기본값은 32명입니다.">?</span></span>
            <input id="cfgMaxPlayers" type="number" min="1" max="100" step="1">
          </label>
          <label>
            <span class="field-title">RCON 포트 <span class="help" tabindex="0" data-tip="RCON을 켰을 때 사용하는 TCP 포트입니다. 기본값은 25575입니다.">?</span></span>
            <input id="cfgRconPort" type="number" min="1" max="65535" step="1">
          </label>
          <div class="checkline">
            <input id="cfgRconEnabled" type="checkbox">
            <label class="check-text" for="cfgRconEnabled">RCON 사용</label>
            <span class="help" tabindex="0" data-tip="외부 관리 도구에서 서버를 제어할 때 사용합니다. 필요할 때만 켜세요.">?</span>
          </div>
          <div class="config-save-wrap">
            <button id="configSaveBtn" onclick="saveConfig()">설정 저장</button>
            <div id="configSaveBubble" class="save-bubble" role="status" aria-live="polite">설정이 저장되었습니다.</div>
          </div>
        </div>
      </div>
        <div class="advanced-card">
          <div class="settings-hub-pane">
            <button id="advancedSettingsBtn" class="advanced-button" type="button" onclick="openAdvancedSettings()" title="상세 설정 열기" aria-label="상세 설정 열기">
              <img src="/static/palworld-settings-icon.png" alt="">
              <span class="advanced-button-label">설정하기</span>
            </button>
            <div class="advanced-copy">
              <div class="advanced-title">Palworld 상세 서버 설정</div>
              <div class="advanced-subtitle">경험치, 포획률, 낮/밤 속도, 알 부화 시간, 전투 배율과 월드 규칙을 조정합니다.</div>
            </div>
          </div>
          <div class="settings-hub-pane">
            <button id="restartSettingsBtn" class="advanced-button" type="button" onclick="openRestartScheduleSettings()" title="자동 재시작 설정 열기" aria-label="자동 재시작 설정 열기">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M20 11a8 8 0 1 0-2.34 5.66" />
                <path d="M20 5v6h-6" />
                <path d="M12 7v5l3 2" />
              </svg>
              <span class="advanced-button-label">예약설정</span>
            </button>
            <div class="advanced-copy">
              <div class="advanced-title">게임 서버 자동 재시작</div>
              <div class="advanced-subtitle">매일 지정한 한국표준 시각에 실행 중인 게임 서버 컨테이너만 재시작합니다.</div>
              <span id="restartScheduleSummary" class="restart-hub-summary">예약 정보 확인 중</span>
            </div>
          </div>
        </div>
    </div>

    <div id="advancedModal" class="modal-backdrop" aria-hidden="true">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="advancedModalTitle">
        <div class="modal-head">
          <h2 id="advancedModalTitle">Palworld 상세 서버 설정</h2>
          <button class="modal-close" type="button" onclick="closeAdvancedSettings()" title="닫기" aria-label="닫기">×</button>
        </div>
        <div id="advancedOptionsBody" class="modal-body"></div>
        <div class="modal-foot">
          <button class="secondary" type="button" onclick="closeAdvancedSettings()">닫기</button>
          <div class="advanced-save-wrap">
            <button id="advancedSaveBtn" type="button" onclick="saveConfig({ source: 'advanced' })">설정 저장</button>
            <div id="advancedSaveBubble" class="save-bubble" role="status" aria-live="polite">저장완료</div>
          </div>
        </div>
      </div>
    </div>

    <div id="restartScheduleModal" class="modal-backdrop" aria-hidden="true">
      <div class="modal restart-modal" role="dialog" aria-modal="true" aria-labelledby="restartScheduleModalTitle">
        <div class="modal-head">
          <h2 id="restartScheduleModalTitle">게임 서버 자동 재시작</h2>
          <button class="modal-close" type="button" onclick="closeRestartScheduleSettings()" title="닫기" aria-label="닫기">×</button>
        </div>
        <div class="restart-modal-body">
          <div class="restart-modal-intro">
            <div class="restart-modal-intro-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 11a8 8 0 1 0-2.34 5.66" />
                <path d="M20 5v6h-6" />
                <path d="M12 7v5l3 2" />
              </svg>
            </div>
            <div>
              <strong>Palworld 게임 컨테이너 예약 관리</strong>
              <p>서버가 실행 중일 때만 재시작하며, 중지 상태에서는 자동으로 기동하지 않습니다.</p>
            </div>
          </div>
          <div class="restart-modal-controls">
            <div class="restart-modal-field">
              <label class="restart-toggle" for="restartScheduleEnabled">
                <input id="restartScheduleEnabled" type="checkbox">
                <span>매일 자동 재시작 사용</span>
              </label>
            </div>
            <label class="restart-modal-field restart-time-field">
              <span>재시작 시각 (KST)</span>
              <input id="restartScheduleTime" type="time" value="04:00" step="60">
            </label>
          </div>
          <div id="restartScheduleStatus" class="restart-schedule-status">예약 정보를 불러오는 중입니다.</div>
        </div>
        <div class="modal-foot">
          <button class="secondary" type="button" onclick="closeRestartScheduleSettings()">취소</button>
          <div class="restart-save-wrap">
            <button id="restartScheduleSaveBtn" type="button" onclick="saveRestartSchedule()">예약 저장</button>
            <div id="restartScheduleSaveBubble" class="save-bubble" role="status" aria-live="polite">저장완료</div>
          </div>
        </div>
      </div>
    </div>

    <div class="actions">
      <button id="installBtn" onclick="requestInstall()">엔진 설치</button>
      <button class="secondary" onclick="startServer()">서버 시작</button>
      <button class="secondary" onclick="stopServer()">서버 중지</button>
      <button class="secondary" onclick="restartServer()">서버 재시작</button>
      <button class="secondary" onclick="loadServerLog()">서버 로그 보기</button>
      <button class="secondary" onclick="loadLog()">설치 로그 보기</button>
      <button class="secondary" onclick="openFileExplorer()">서버 디렉토리 탐색기</button>
      <input id="fileExplorerUploadInput" type="file" onchange="uploadExplorerFile()" style="display:none">
    </div>

    <div id="fileExplorerModal" class="modal-backdrop" aria-hidden="true">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="fileExplorerTitle">
        <div class="modal-head">
          <h2 id="fileExplorerTitle">서버 디렉토리 탐색기</h2>
          <button class="modal-close" type="button" onclick="closeFileExplorer()" title="닫기" aria-label="닫기">×</button>
        </div>
        <div class="modal-body">
          <div class="explorer-toolbar">
            <div id="fileExplorerPath" class="explorer-path">/Saved</div>
            <div class="explorer-actions">
              <button class="secondary" type="button" onclick="loadFileExplorer()">새로고침</button>
              <button id="fileExplorerUpBtn" class="secondary" type="button" onclick="goFileExplorerParent()">상위 폴더</button>
              <button id="fileExplorerNewDirBtn" class="secondary" type="button" onclick="createExplorerDirectory()">폴더 생성</button>
              <button id="fileExplorerUploadBtn" type="button" onclick="triggerExplorerUpload()">파일 업로드</button>
            </div>
          </div>
          <table class="explorer-table">
            <thead>
              <tr>
                <th>이름</th>
                <th>종류</th>
                <th>크기</th>
                <th>수정일</th>
                <th style="text-align:right">작업</th>
              </tr>
            </thead>
            <tbody id="fileExplorerBody"></tbody>
          </table>
          <div id="fileExplorerNote" class="explorer-note"></div>
        </div>
      </div>
    </div>

    <div id="result" class="result" hidden></div>

  </div>

  <script>
    let currentLogMode = "install";
    let advancedOptions = {};

    const advancedOptionGroups = [
      {
        title: "접속/플랫폼",
        fields: [
          { key: "CoopPlayerMaxNum", label: "협동 플레이 인원", type: "number", step: "1", min: "1", max: "32" },
          { key: "PublicIP", label: "공개 IP", type: "text" },
          { key: "Region", label: "서버 지역", type: "text" },
          { key: "CrossplayPlatforms", label: "크로스플레이 플랫폼", type: "text" },
          { key: "BanListURL", label: "밴 목록 URL", type: "text" },
          { key: "bUseAuth", label: "서버 인증 사용", type: "checkbox" },
          { key: "bIsMultiplay", label: "멀티플레이 모드", type: "checkbox" }
        ]
      },
      {
        title: "배율",
        fields: [
          { key: "DayTimeSpeedRate", label: "낮 시간 속도", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "NightTimeSpeedRate", label: "밤 시간 속도", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "ExpRate", label: "경험치 배율", type: "number", step: "0.1", min: "0.1", max: "20" },
          { key: "PalCaptureRate", label: "포획률", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "PalSpawnNumRate", label: "팰 출현 배율", type: "number", step: "0.1", min: "0.1", max: "3" },
          { key: "CollectionDropRate", label: "채집 드롭 배율", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "EnemyDropItemRate", label: "적 드롭 배율", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "WorkSpeedRate", label: "작업 속도", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "PalEggDefaultHatchingTime", label: "알 부화 시간", type: "number", step: "0.1", min: "0", max: "240" }
        ]
      },
      {
        title: "회복/건축/채집",
        fields: [
          { key: "PlayerAutoHPRegeneRate", label: "플레이어 HP 회복", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "PlayerAutoHpRegeneRateInSleep", label: "플레이어 수면 HP 회복", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "PalAutoHPRegeneRate", label: "팰 HP 회복", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "PalAutoHpRegeneRateInSleep", label: "팰 수면 HP 회복", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "BuildObjectHpRate", label: "건축물 HP 배율", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "BuildObjectDamageRate", label: "건축물 피해 배율", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "BuildObjectDeteriorationDamageRate", label: "건축물 열화 피해", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "CollectionObjectHpRate", label: "채집 오브젝트 HP", type: "number", step: "0.1", min: "0.1", max: "10" },
          { key: "CollectionObjectRespawnSpeedRate", label: "채집 리스폰 속도", type: "number", step: "0.1", min: "0.1", max: "10" }
        ]
      },
      {
        title: "전투",
        fields: [
          { key: "PlayerDamageRateAttack", label: "플레이어 공격 배율", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "PlayerDamageRateDefense", label: "플레이어 방어 배율", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "PalDamageRateAttack", label: "팰 공격 배율", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "PalDamageRateDefense", label: "팰 방어 배율", type: "number", step: "0.1", min: "0.1", max: "5" },
          { key: "DeathPenalty", label: "사망 패널티", type: "select", options: ["None", "Item", "ItemAndEquipment", "All"] },
          { key: "bEnablePlayerToPlayerDamage", label: "플레이어 간 피해", type: "checkbox" },
          { key: "bEnableFriendlyFire", label: "아군 피해", type: "checkbox" },
          { key: "bIsPvP", label: "PvP 모드", type: "checkbox" },
          { key: "bHardcore", label: "하드코어", type: "checkbox" },
          { key: "bCharacterRecreateInHardcore", label: "하드코어 캐릭터 재생성", type: "checkbox" },
          { key: "bEnableInvaderEnemy", label: "습격 이벤트", type: "checkbox" },
          { key: "bActiveUNKO", label: "UNKO 활성화", type: "checkbox" }
        ]
      },
      {
        title: "생존/이동",
        fields: [
          { key: "PlayerStomachDecreaceRate", label: "플레이어 포만감 감소", type: "number", step: "0.1", min: "0", max: "5" },
          { key: "PlayerStaminaDecreaceRate", label: "플레이어 스태미나 감소", type: "number", step: "0.1", min: "0", max: "5" },
          { key: "PalStomachDecreaceRate", label: "팰 포만감 감소", type: "number", step: "0.1", min: "0", max: "5" },
          { key: "PalStaminaDecreaceRate", label: "팰 스태미나 감소", type: "number", step: "0.1", min: "0", max: "5" },
          { key: "ItemWeightRate", label: "아이템 무게 배율", type: "number", step: "0.1", min: "0", max: "10" },
          { key: "bEnableFastTravel", label: "빠른 이동 허용", type: "checkbox" },
          { key: "bEnableFastTravelOnlyBaseCamp", label: "거점 빠른 이동만 허용", type: "checkbox" },
          { key: "EnablePredatorBossPal", label: "프레데터 보스 팰", type: "checkbox" },
          { key: "bPalLost", label: "팰 손실", type: "checkbox" },
          { key: "bEnableAimAssistPad", label: "패드 조준 보정", type: "checkbox" },
          { key: "bEnableAimAssistKeyboard", label: "키보드 조준 보정", type: "checkbox" },
          { key: "bIsStartLocationSelectByMap", label: "지도에서 시작 위치 선택", type: "checkbox" },
          { key: "bExistPlayerAfterLogout", label: "로그아웃 후 캐릭터 유지", type: "checkbox" }
        ]
      },
      {
        title: "거점/길드",
        fields: [
          { key: "BaseCampMaxNum", label: "전체 거점 최대 수", type: "number", step: "1", min: "1", max: "512" },
          { key: "BaseCampMaxNumInGuild", label: "길드 거점 최대 수", type: "number", step: "1", min: "1", max: "10" },
          { key: "BaseCampWorkerMaxNum", label: "거점 작업 팰 수", type: "number", step: "1", min: "1", max: "50" },
          { key: "GuildPlayerMaxNum", label: "길드 최대 인원", type: "number", step: "1", min: "1", max: "100" },
          { key: "bAutoResetGuildNoOnlinePlayers", label: "미접속 길드 자동 초기화", type: "checkbox" },
          { key: "AutoResetGuildTimeNoOnlinePlayers", label: "길드 초기화 시간", type: "number", step: "1", min: "1", max: "720" },
          { key: "bAllowGlobalPalboxExport", label: "글로벌 팰박스 내보내기", type: "checkbox" },
          { key: "bAllowGlobalPalboxImport", label: "글로벌 팰박스 가져오기", type: "checkbox" },
          { key: "MaxBuildingLimitNum", label: "건축물 제한 수", type: "number", step: "1", min: "0", max: "10000" },
          { key: "bBuildAreaLimit", label: "건축 구역 제한", type: "checkbox" },
          { key: "bCanPickupOtherGuildDeathPenaltyDrop", label: "타 길드 사망 드롭 줍기", type: "checkbox" },
          { key: "bEnableDefenseOtherGuildPlayer", label: "타 길드 방어 허용", type: "checkbox" },
          { key: "bInvisibleOtherGuildBaseCampAreaFX", label: "타 길드 거점 표시 숨김", type: "checkbox" }
        ]
      },
      {
        title: "운영",
        fields: [
          { key: "AutoSaveSpan", label: "자동 저장 간격", type: "number", step: "1", min: "1", max: "300" },
          { key: "SupplyDropSpan", label: "보급품 드롭 간격", type: "number", step: "1", min: "0", max: "720" },
          { key: "ChatPostLimitPerMinute", label: "분당 채팅 제한", type: "number", step: "1", min: "1", max: "120" },
          { key: "DropItemMaxNum", label: "드롭 아이템 최대 수", type: "number", step: "1", min: "0", max: "10000" },
          { key: "DropItemMaxNum_UNKO", label: "UNKO 드롭 최대 수", type: "number", step: "1", min: "0", max: "1000" },
          { key: "DropItemAliveMaxHours", label: "드롭 아이템 유지 시간", type: "number", step: "0.1", min: "0", max: "24" },
          { key: "ServerReplicatePawnCullDistance", label: "서버 복제 거리", type: "number", step: "100", min: "1000", max: "50000" },
          { key: "bEnableNonLoginPenalty", label: "미접속 패널티", type: "checkbox" },
          { key: "bIsUseBackupSaveData", label: "공식 백업 저장 사용", type: "checkbox" },
          { key: "bShowPlayerList", label: "플레이어 목록 표시", type: "checkbox" },
          { key: "bIsShowJoinLeftMessage", label: "입장/퇴장 메시지", type: "checkbox" },
          { key: "bAllowClientMod", label: "클라이언트 모드 허용", type: "checkbox" },
          { key: "LogFormatType", label: "로그 형식", type: "select", options: ["Text", "Json"] }
        ]
      },
      {
        title: "랜덤라이저",
        fields: [
          { key: "RandomizerType", label: "랜덤라이저 방식", type: "select", options: ["None", "Region", "All"] },
          { key: "RandomizerSeed", label: "랜덤라이저 시드", type: "text" },
          { key: "bIsRandomizerPalLevelRandom", label: "팰 레벨 랜덤", type: "checkbox" }
        ]
      },
      {
        title: "REST API",
        fields: [
          { key: "RESTAPIEnabled", label: "REST API 활성화", type: "checkbox" },
          { key: "RESTAPIPort", label: "REST API 포트", type: "number", step: "1", min: "1", max: "65535" }
        ]
      }
    ];

    function getLogBox() {
      return document.getElementById("installLog");
    }

    function scrollLogToBottom() {
      const logBox = getLogBox();
      logBox.scrollTop = logBox.scrollHeight;
    }

    function setLogText(text) {
      const logBox = getLogBox();
      logBox.innerText = text || "로그가 없습니다.";
      scrollLogToBottom();
    }

    const statusIcons = {
      check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.2 4.2L19 7" /></svg>',
      x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7 17 17" /><path d="m17 7-10 10" /></svg>',
      pending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6v6l4 2" /><circle cx="12" cy="12" r="8" /></svg>',
      server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="5.5" rx="1.4" /><rect x="4" y="14.5" width="16" height="5.5" rx="1.4" /><path d="M7 6.8h.01" /><path d="M7 17.3h.01" /><path d="M11 9.5v5" /><path d="M14.5 12h5" /><path d="m17.8 9.8 2.2 2.2-2.2 2.2" /></svg>'
    };

    function setStatusIcon(iconId, variant, iconName) {
      const icon = document.getElementById(iconId);

      if (!icon) {
        return;
      }

      icon.classList.remove("status-ok", "status-bad", "status-pending", "server-live");
      icon.classList.add(variant);
      icon.innerHTML = statusIcons[iconName] || statusIcons.pending;
    }

    function updateInstallStatusIcon(status) {
      const normalized = (status || "").toLowerCase();

      if (normalized === "completed") {
        setStatusIcon("installStatusIcon", "status-ok", "check");
      } else if (["not_started", "failed", "error", "update_required"].includes(normalized)) {
        setStatusIcon("installStatusIcon", "status-bad", "x");
      } else {
        setStatusIcon("installStatusIcon", "status-pending", "pending");
      }
    }

    function updateServerStatusIcon(status) {
      const normalized = (status || "").toLowerCase();

      if (normalized === "running") {
        setStatusIcon("serverStatusIcon", "server-live", "server");
      } else if (["not_created", "stopped", "exited", "dead", "error", "config_error", "outdated"].includes(normalized)) {
        setStatusIcon("serverStatusIcon", "status-bad", "x");
      } else {
        setStatusIcon("serverStatusIcon", "status-pending", "pending");
      }
    }

    function isRunningStatus(status) {
      return (status || "").toLowerCase() === "running";
    }

    function displayInstallStatus(status) {
      const normalized = (status || "").toLowerCase();
      const labels = {
        completed: "설치 완료",
        started: "설치 중",
        installing: "설치 중",
        running: "설치 중",
        pending: "대기 중",
        not_started: "설치 전",
        update_required: "1.0 설치 필요",
        failed: "설치 실패",
        error: "오류"
      };
      return labels[normalized] || status || "확인 중";
    }

    function displayServerStatus(status) {
      const normalized = (status || "").toLowerCase();
      const labels = {
        running: "실행 중",
        starting: "시작 중",
        started: "시작됨",
        stopping: "중지 중",
        stopped: "중지됨",
        restarting: "재시작 중",
        created: "생성됨",
        exited: "종료됨",
        dead: "비정상 종료",
        not_created: "생성 전",
        outdated: "교체 필요",
        config_error: "설정 오류",
        error: "오류"
      };
      return labels[normalized] || status || "확인 중";
    }

    function setConfigLocked(locked) {
      const section = document.getElementById("configSection");
      if (section) {
        section.classList.toggle("locked", locked);
      }

      [
        "cfgServerName",
        "cfgDescription",
        "cfgAdminPassword",
        "cfgPassword",
        "cfgPort",
        "cfgMaxPlayers",
        "cfgRconEnabled",
        "cfgRconPort",
        "configSaveBtn",
        "advancedSettingsBtn",
        "advancedSaveBtn"
      ].forEach(function (id) {
        const element = document.getElementById(id);
        if (element) {
          element.disabled = locked;
        }
      });

      document.querySelectorAll("[data-advanced-key]").forEach(function (element) {
        element.disabled = locked;
      });

      document.querySelectorAll("[data-advanced-range]").forEach(function (element) {
        element.disabled = locked;
      });
    }

    async function requestInstall() {
      const btn = document.getElementById("installBtn");
      const result = document.getElementById("result");

      currentLogMode = "install";
      btn.disabled = true;
      result.innerText = "Palworld 엔진 설치 작업을 시작하는 중입니다...";

      try {
        const response = await fetch("/api/install", {
          method: "POST"
        });

        const data = await response.json();

        if (!response.ok) {
          result.innerText = "오류: " + (data.detail || "설치 요청 실패");
          return;
        }

        result.innerText = "";

        await loadStatus();
        await loadLog();
      } catch (err) {
        result.innerText = "요청 실패: " + err;
      } finally {
        btn.disabled = false;
      }
    }

    async function startServer() {
      const result = document.getElementById("result");
      const currentStatus = await loadServerStatus();

      if (isRunningStatus(currentStatus)) {
        alert("이미 서버가 동작중입니다.");
        currentLogMode = "server";
        await loadServerLog({ preserveWhenEmpty: true });
        return;
      }

      result.innerText = "Palworld 서버를 시작하는 중입니다...";
      currentLogMode = "server";
      setLogText("[패널] Palworld 서버를 시작하는 중입니다...");

      try {
        const response = await fetch("/api/server/start", {
          method: "POST"
        });

        const data = await response.json();

        if (isRunningStatus(data.status)) {
          alert("이미 서버가 동작중입니다.");
        }

        result.innerText =
          "서버 시작 요청 결과\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + (data.message || "") + "\\n" +
          (data.worlds && data.worlds.length ? "기존 월드: " + data.worlds.join(", ") + "\\n" : "") +
          (data.port ? "포트: " + data.port + "\\n" : "") +
          (data.container ? "컨테이너: " + data.container : "");

        if (data.status === "config_error") {
          currentLogMode = "server";
          setLogText("[패널] 서버 시작이 차단되었습니다.\\n" + (data.message || ""));
          return;
        }

        currentLogMode = "server";
        await loadServerStatus();
        await loadServerLog({ preserveWhenEmpty: true });

      } catch (err) {
        result.innerText = "서버 시작 요청 실패: " + err;
      }
    }

    async function stopServer() {
      const result = document.getElementById("result");

      result.innerText = "Palworld 서버를 중지하는 중입니다...";
      currentLogMode = "server";
      document.getElementById("serverStatus").innerText = displayServerStatus("stopping");
      updateServerStatusIcon("stopping");
      setLogText("[패널] Palworld 서버 중지 요청을 보냈습니다...");

      try {
        const response = await fetch("/api/server/stop", {
          method: "POST"
        });

        const data = await response.json();

        result.innerText =
          "서버 중지 요청 결과\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + (data.message || "") + "\\n" +
          (data.container ? "컨테이너: " + data.container : "");

        currentLogMode = "server";
        if (data.status === "stopped" || data.status === "not_created") {
          document.getElementById("serverStatus").innerText = displayServerStatus(data.status);
          updateServerStatusIcon(data.status);
          setConfigLocked(false);
        }
        await loadServerStatus();
        setLogText(data.log || ("[패널] " + (data.message || "Palworld 서버가 종료되었습니다.")));

      } catch (err) {
        result.innerText = "서버 중지 요청 실패: " + err;
      }
    }

    async function restartServer() {
      const result = document.getElementById("result");

      result.innerText = "Palworld 서버를 재시작하는 중입니다...";

      try {
        const response = await fetch("/api/server/restart", {
          method: "POST"
        });

        const data = await response.json();

        result.innerText =
          "서버 재시작 요청 결과\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + (data.message || "") + "\\n" +
          (data.container ? "컨테이너: " + data.container : "");

        currentLogMode = "server";
        await loadServerStatus();
        await loadServerLog();

      } catch (err) {
        result.innerText = "서버 재시작 요청 실패: " + err;
      }
    }

    let fileExplorerPath = "";
    let fileExplorerLocked = false;

    function formatFileSize(size) {
      const bytes = Number(size || 0);

      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
      return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
    }

    function setFileExplorerWriteLocked(locked) {
      fileExplorerLocked = locked;
      ["fileExplorerNewDirBtn", "fileExplorerUploadBtn"].forEach(function (id) {
        const element = document.getElementById(id);
        if (element) {
          element.disabled = locked;
        }
      });

      const note = document.getElementById("fileExplorerNote");
      if (note) {
        note.innerText = locked
          ? "서버 실행 중에는 안전을 위해 업로드, 삭제, 폴더 생성을 막습니다. 다운로드와 탐색은 가능합니다."
          : "Saved 폴더 안에서만 탐색, 업로드, 다운로드, 삭제가 가능합니다.";
      }
    }

    async function openFileExplorer() {
      const modal = document.getElementById("fileExplorerModal");
      modal.classList.add("show");
      modal.setAttribute("aria-hidden", "false");
      await loadFileExplorer(fileExplorerPath || "");
    }

    function closeFileExplorer() {
      const modal = document.getElementById("fileExplorerModal");
      modal.classList.remove("show");
      modal.setAttribute("aria-hidden", "true");
    }

    async function loadFileExplorer(path) {
      if (path !== undefined) {
        fileExplorerPath = path;
      }

      const body = document.getElementById("fileExplorerBody");
      body.innerHTML = '<tr><td colspan="5">목록을 불러오는 중입니다...</td></tr>';

      try {
        const response = await fetch("/api/files?path=" + encodeURIComponent(fileExplorerPath || ""));
        const data = await response.json();

        if (!response.ok) {
          body.innerHTML = '<tr><td colspan="5">오류: ' + (data.detail || "목록 조회 실패") + '</td></tr>';
          return;
        }

        fileExplorerPath = data.path || "";
        document.getElementById("fileExplorerPath").innerText = "/Saved" + (fileExplorerPath ? "/" + fileExplorerPath : "");
        document.getElementById("fileExplorerUpBtn").disabled = !fileExplorerPath;
        setFileExplorerWriteLocked(Boolean(data.write_locked));
        renderFileExplorerEntries(data.entries || []);
      } catch (err) {
        body.innerHTML = '<tr><td colspan="5">목록 조회 실패: ' + err + '</td></tr>';
      }
    }

    function renderFileExplorerEntries(entries) {
      const body = document.getElementById("fileExplorerBody");
      body.innerHTML = "";

      if (!entries.length) {
        body.innerHTML = '<tr><td colspan="5">폴더가 비어 있습니다.</td></tr>';
        return;
      }

      entries.forEach(function (entry) {
        const row = document.createElement("tr");
        const nameCell = document.createElement("td");
        const nameButton = document.createElement("button");
        nameButton.className = "explorer-name" + (entry.type === "file" ? " file" : "");
        nameButton.type = "button";
        nameButton.innerText = (entry.type === "dir" ? "📁 " : "📄 ") + entry.name;

        if (entry.type === "dir") {
          nameButton.onclick = function () { loadFileExplorer(entry.path); };
        } else {
          nameButton.disabled = true;
        }

        nameCell.appendChild(nameButton);
        row.appendChild(nameCell);

        const typeCell = document.createElement("td");
        typeCell.innerText = entry.type === "dir" ? "폴더" : "파일";
        row.appendChild(typeCell);

        const sizeCell = document.createElement("td");
        sizeCell.innerText = entry.type === "file" ? formatFileSize(entry.size) : "-";
        row.appendChild(sizeCell);

        const modifiedCell = document.createElement("td");
        modifiedCell.innerText = entry.modified || "";
        row.appendChild(modifiedCell);

        const actionCell = document.createElement("td");
        actionCell.className = "explorer-row-actions";

        if (entry.type === "file") {
          const downloadButton = document.createElement("button");
          downloadButton.className = "secondary";
          downloadButton.type = "button";
          downloadButton.innerText = "다운로드";
          downloadButton.onclick = function () { downloadExplorerFile(entry.path); };
          actionCell.appendChild(downloadButton);
        }

        const deleteButton = document.createElement("button");
        deleteButton.className = "danger";
        deleteButton.type = "button";
        deleteButton.innerText = "삭제";
        deleteButton.disabled = fileExplorerLocked;
        deleteButton.onclick = function () { deleteExplorerEntry(entry.path); };
        actionCell.appendChild(deleteButton);

        row.appendChild(actionCell);
        body.appendChild(row);
      });
    }

    function goFileExplorerParent() {
      if (!fileExplorerPath) {
        return;
      }

      const parts = fileExplorerPath.split("/").filter(Boolean);
      parts.pop();
      loadFileExplorer(parts.join("/"));
    }

    function downloadExplorerFile(path) {
      window.location.href = "/api/files/download?path=" + encodeURIComponent(path);
    }

    function triggerExplorerUpload() {
      if (fileExplorerLocked) {
        return;
      }

      document.getElementById("fileExplorerUploadInput").click();
    }

    async function uploadExplorerFile() {
      const input = document.getElementById("fileExplorerUploadInput");

      if (!input.files || input.files.length === 0) {
        return;
      }

      const formData = new FormData();
      formData.append("file", input.files[0]);

      try {
        const response = await fetch("/api/files/upload?path=" + encodeURIComponent(fileExplorerPath || ""), {
          method: "POST",
          body: formData
        });
        const data = await response.json();

        if (!response.ok) {
          alert(data.detail || "파일 업로드 실패");
          return;
        }

        await loadFileExplorer(fileExplorerPath);
      } catch (err) {
        alert("파일 업로드 실패: " + err);
      } finally {
        input.value = "";
      }
    }

    async function createExplorerDirectory() {
      if (fileExplorerLocked) {
        return;
      }

      const name = window.prompt("생성할 폴더 이름을 입력해주세요.");

      if (!name) {
        return;
      }

      try {
        const response = await fetch("/api/files/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: fileExplorerPath || "", name: name })
        });
        const data = await response.json();

        if (!response.ok) {
          alert(data.detail || "폴더 생성 실패");
          return;
        }

        await loadFileExplorer(fileExplorerPath);
      } catch (err) {
        alert("폴더 생성 실패: " + err);
      }
    }

    async function deleteExplorerEntry(path) {
      if (fileExplorerLocked || !window.confirm("선택한 항목을 삭제할까요?")) {
        return;
      }

      try {
        const response = await fetch("/api/files/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path })
        });
        const data = await response.json();

        if (!response.ok) {
          alert(data.detail || "삭제 실패");
          return;
        }

        await loadFileExplorer(fileExplorerPath);
      } catch (err) {
        alert("삭제 실패: " + err);
      }
    }

    function updateRangeFill(range) {
      const min = Number(range.min || 0);
      const max = Number(range.max || 100);
      const value = Number(range.value || min);
      const percent = max > min ? ((value - min) / (max - min)) * 100 : 0;
      range.style.setProperty("--range-fill", Math.max(0, Math.min(100, percent)) + "%");
    }

    function clampNumberInput(element) {
      let value = Number(element.value || 0);

      if (element.min !== "") {
        value = Math.max(Number(element.min), value);
      }

      if (element.max !== "") {
        value = Math.min(Number(element.max), value);
      }

      element.value = value;
      return value;
    }

    function buildAdvancedOptions() {
      const body = document.getElementById("advancedOptionsBody");

      if (!body || body.dataset.ready === "true") {
        return;
      }

      advancedOptionGroups.forEach(function (group) {
        const groupEl = document.createElement("section");
        groupEl.className = "advanced-group";

        const title = document.createElement("h3");
        title.innerText = group.title;
        groupEl.appendChild(title);

        const grid = document.createElement("div");
        grid.className = "advanced-grid";

        group.fields.forEach(function (field) {
          let wrapper;
          let input;

          if (field.type === "checkbox") {
            wrapper = document.createElement("div");
            wrapper.className = "checkline advanced-check";

            input = document.createElement("input");
            input.type = "checkbox";
            input.id = "adv_" + field.key;
            input.dataset.advancedKey = field.key;
            input.dataset.advancedType = field.type;

            const label = document.createElement("label");
            label.className = "check-text";
            label.htmlFor = input.id;
            label.innerText = field.label;

            wrapper.appendChild(input);
            wrapper.appendChild(label);
          } else {
            wrapper = document.createElement("label");

            const title = document.createElement("span");
            title.className = "field-title";
            title.innerText = field.label;
            wrapper.appendChild(title);

            if (field.type === "select") {
              input = document.createElement("select");
              (field.options || []).forEach(function (optionValue) {
                const option = document.createElement("option");
                option.value = optionValue;
                option.innerText = optionValue;
                input.appendChild(option);
              });
            } else if (field.type === "number" && field.max !== undefined) {
              wrapper.className = "range-field";

              const row = document.createElement("div");
              row.className = "range-row";

              const range = document.createElement("input");
              range.type = "range";
              range.className = "range-slider";
              range.id = "adv_range_" + field.key;
              range.dataset.advancedRange = field.key;
              range.step = field.step || "1";
              range.min = field.min !== undefined ? field.min : "0";
              range.max = field.max;

              input = document.createElement("input");
              input.type = "number";
              input.className = "range-number";
              if (field.step !== undefined) input.step = field.step;
              if (field.min !== undefined) input.min = field.min;
              if (field.max !== undefined) input.max = field.max;

              range.addEventListener("input", function () {
                input.value = range.value;
                updateRangeFill(range);
              });

              input.addEventListener("input", function () {
                if (input.value === "") {
                  return;
                }

                range.value = input.value;
                updateRangeFill(range);
              });

              input.addEventListener("change", function () {
                input.value = clampNumberInput(input);
                range.value = input.value;
                updateRangeFill(range);
              });

              row.appendChild(range);
              row.appendChild(input);
              wrapper.appendChild(row);
            } else {
              input = document.createElement("input");
              input.type = field.type || "text";
              if (field.step !== undefined) input.step = field.step;
              if (field.min !== undefined) input.min = field.min;
              if (field.max !== undefined) input.max = field.max;
              wrapper.appendChild(input);
            }

            input.id = "adv_" + field.key;
            input.dataset.advancedKey = field.key;
            input.dataset.advancedType = field.type || "text";
            if (!input.parentNode) {
              wrapper.appendChild(input);
            }
          }

          grid.appendChild(wrapper);
        });

        groupEl.appendChild(grid);
        body.appendChild(groupEl);
      });

      body.dataset.ready = "true";
    }

    function fillAdvancedOptions(options) {
      advancedOptions = Object.assign({}, options || {});
      buildAdvancedOptions();

      document.querySelectorAll("[data-advanced-key]").forEach(function (element) {
        const key = element.dataset.advancedKey;
        const value = advancedOptions[key];

        if (element.dataset.advancedType === "checkbox") {
          element.checked = Boolean(value);
        } else if (value !== undefined && value !== null) {
          element.value = value;
        }

        if (element.dataset.advancedType === "number") {
          const range = document.querySelector('[data-advanced-range="' + key + '"]');
          if (range) {
            range.value = element.value;
            updateRangeFill(range);
          }
        }
      });
    }

    function readAdvancedOptions() {
      const options = Object.assign({}, advancedOptions);

      document.querySelectorAll("[data-advanced-key]").forEach(function (element) {
        const key = element.dataset.advancedKey;

        if (element.dataset.advancedType === "checkbox") {
          options[key] = element.checked;
        } else if (element.dataset.advancedType === "number") {
          options[key] = clampNumberInput(element);
          const range = document.querySelector('[data-advanced-range="' + key + '"]');
          if (range) {
            range.value = options[key];
            updateRangeFill(range);
          }
        } else {
          options[key] = element.value;
        }
      });

      return options;
    }

    function openAdvancedSettings() {
      if (document.getElementById("configSection").classList.contains("locked")) {
        return;
      }

      buildAdvancedOptions();
      fillAdvancedOptions(advancedOptions);

      const modal = document.getElementById("advancedModal");
      modal.classList.add("show");
      modal.setAttribute("aria-hidden", "false");
    }

    function closeAdvancedSettings() {
      const modal = document.getElementById("advancedModal");
      modal.classList.remove("show");
      modal.setAttribute("aria-hidden", "true");
    }

    function fillConfig(config) {
      document.getElementById("cfgServerName").value = config.ServerName || "TechTim Palworld Server";
      document.getElementById("cfgDescription").value = config.ServerDescription || "";
      document.getElementById("cfgAdminPassword").value = config.AdminPassword || "";
      document.getElementById("cfgPassword").value = config.ServerPassword || "";
      document.getElementById("cfgPort").value = config.PublicPort || 8211;
      document.getElementById("cfgMaxPlayers").value = config.MaxPlayers || 32;
      document.getElementById("cfgRconEnabled").checked = Boolean(config.RCONEnabled);
      document.getElementById("cfgRconPort").value = config.RCONPort || 25575;
      fillAdvancedOptions(config.AdvancedOptions || {});
    }

    function fillWorldList(worlds) {
      const list = document.getElementById("worldNameList");

      if (!list) {
        return;
      }

      list.innerHTML = "";

      (worlds || []).forEach(function (world) {
        const option = document.createElement("option");
        option.value = world;
        list.appendChild(option);
      });
    }

    function addWorldOption(worldName) {
      const list = document.getElementById("worldNameList");
      const normalized = (worldName || "").trim();

      if (!list) {
        return;
      }

      if (!normalized) {
        return;
      }

      const exists = Array.from(list.options).some(function (option) {
        return option.value === normalized;
      });

      if (exists) {
        return;
      }

      const option = document.createElement("option");
      option.value = normalized;
      list.appendChild(option);
    }

    let configSaveBubbleTimer = null;
    let advancedSaveBubbleTimer = null;

    function showSaveBubble(bubbleId, durationMs, onDone) {
      const bubble = document.getElementById(bubbleId);

      if (!bubble) {
        return;
      }

      const isAdvancedBubble = bubbleId === "advancedSaveBubble";
      window.clearTimeout(isAdvancedBubble ? advancedSaveBubbleTimer : configSaveBubbleTimer);
      bubble.classList.add("show");

      const timer = window.setTimeout(function () {
        bubble.classList.remove("show");
        if (onDone) {
          onDone();
        }
      }, durationMs);

      if (isAdvancedBubble) {
        advancedSaveBubbleTimer = timer;
      } else {
        configSaveBubbleTimer = timer;
      }
    }

    function showConfigSaveBubble() {
      showSaveBubble("configSaveBubble", 2200);
    }

    function showAdvancedSaveBubbleAndClose() {
      showSaveBubble("advancedSaveBubble", 1000, closeAdvancedSettings);
    }

    let restartScheduleSaveBubbleTimer = null;

    function showRestartScheduleSaveBubble() {
      const bubble = document.getElementById("restartScheduleSaveBubble");

      window.clearTimeout(restartScheduleSaveBubbleTimer);
      bubble.classList.add("show");
      restartScheduleSaveBubbleTimer = window.setTimeout(function () {
        bubble.classList.remove("show");
        closeRestartScheduleSettings();
      }, 1000);
    }

    async function openRestartScheduleSettings() {
      await loadRestartSchedule(true);
      const modal = document.getElementById("restartScheduleModal");
      modal.classList.add("show");
      modal.setAttribute("aria-hidden", "false");
    }

    function closeRestartScheduleSettings() {
      const modal = document.getElementById("restartScheduleModal");
      modal.classList.remove("show");
      modal.setAttribute("aria-hidden", "true");
    }

    function restartScheduleResultLabel(result) {
      const labels = {
        not_run: "실행 기록 없음",
        running: "처리 중",
        success: "재시작 완료",
        skipped: "서버 중지 상태로 건너뜀",
        error: "재시작 오류"
      };
      return labels[result] || result || "실행 기록 없음";
    }

    function renderRestartSchedule(schedule, fillControls) {
      const enabled = Boolean(schedule && schedule.enabled);
      const restartTime = (schedule && schedule.restart_time) || "04:00";
      const summary = document.getElementById("restartScheduleSummary");

      if (summary) {
        summary.innerText = enabled ? "매일 " + restartTime + " KST" : "자동 재시작 꺼짐";
      }

      if (fillControls !== false) {
        document.getElementById("restartScheduleEnabled").checked = enabled;
        document.getElementById("restartScheduleTime").value = restartTime;
      }

      const status = document.getElementById("restartScheduleStatus");

      if (!enabled) {
        status.innerText = "자동 재시작이 비활성화되어 있습니다.";
        return;
      }

      const nextRun = schedule.next_run_at
        ? schedule.next_run_at.replace("T", " ").slice(0, 16) + " KST"
        : "계산 중";
      const lastResult = restartScheduleResultLabel(schedule.last_result);
      const lastRun = schedule.last_run_at
        ? schedule.last_run_at.replace("T", " ").slice(0, 19) + " KST"
        : "없음";

      status.innerText = "다음 재시작: " + nextRun + " · 마지막 결과: " + lastResult + " (" + lastRun + ")";
    }

    async function loadRestartSchedule(fillControls) {
      try {
        const response = await fetch("/api/restart-schedule");
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail || "예약 조회 실패");
        }

        renderRestartSchedule(data.schedule || {}, fillControls);
      } catch (err) {
        document.getElementById("restartScheduleStatus").innerText = "예약 조회 실패: " + err;
        document.getElementById("restartScheduleSummary").innerText = "예약 조회 오류";
      }
    }

    async function saveRestartSchedule() {
      const button = document.getElementById("restartScheduleSaveBtn");
      const payload = {
        enabled: document.getElementById("restartScheduleEnabled").checked,
        restart_time: document.getElementById("restartScheduleTime").value || "04:00"
      };

      button.disabled = true;

      try {
        const response = await fetch("/api/restart-schedule", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail || "예약 저장 실패");
        }

        renderRestartSchedule(data.schedule || {}, true);
        showRestartScheduleSaveBubble();
      } catch (err) {
        document.getElementById("restartScheduleStatus").innerText = "예약 저장 실패: " + err;
      } finally {
        button.disabled = false;
      }
    }

    function readConfigForm() {
      return {
        ServerName: document.getElementById("cfgServerName").value.trim() || "TechTim Palworld Server",
        ServerDescription: document.getElementById("cfgDescription").value,
        AdminPassword: document.getElementById("cfgAdminPassword").value,
        ServerPassword: document.getElementById("cfgPassword").value,
        PublicPort: Number(document.getElementById("cfgPort").value || 8211),
        MaxPlayers: Number(document.getElementById("cfgMaxPlayers").value || 10),
        RCONEnabled: document.getElementById("cfgRconEnabled").checked,
        RCONPort: Number(document.getElementById("cfgRconPort").value || 25575),
        AdvancedOptions: readAdvancedOptions()
      };
    }

    async function loadConfig() {
      try {
        const response = await fetch("/api/config");
        const data = await response.json();
        fillConfig(data.config || {});
        await loadWorlds();
      } catch (err) {
        document.getElementById("result").innerText = "설정 불러오기 실패: " + err;
      }
    }

    async function loadWorlds() {
      try {
        const response = await fetch("/api/worlds");
        const data = await response.json();
        fillWorldList(data.worlds || []);
      } catch (err) {
        fillWorldList([]);
      }
    }

    async function saveConfig(options) {
      const result = document.getElementById("result");
      const isAdvancedSave = Boolean(options && options.source === "advanced");

      if (document.getElementById("configSection").classList.contains("locked")) {
        return;
      }

      result.innerText = "서버 설정을 저장하는 중입니다...";

      try {
        const response = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(readConfigForm())
        });

        const data = await response.json();

        if (!response.ok) {
          result.innerText = "설정 저장 실패: " + (data.detail || data.error || "알 수 없는 오류");
          return;
        }

        fillConfig(data.config || {});
        fillWorldList(data.worlds || []);
        if (isAdvancedSave) {
          showAdvancedSaveBubbleAndClose();
        } else {
          showConfigSaveBubble();
        }
        await loadServerStatus();
        result.innerText = "설정 저장 완료\\n저장 위치: " + data.path;
      } catch (err) {
        result.innerText = "설정 저장 실패: " + err;
      }
    }

    async function loadServerLog(options) {
      currentLogMode = "server";
      const preserveWhenEmpty = Boolean(options && options.preserveWhenEmpty);

      try {
        const response = await fetch("/api/server/log");
        const data = await response.json();
        const logText = data.log || data.error || "";

        if (logText) {
          setLogText(logText);
          return;
        }

        if (preserveWhenEmpty) {
          return;
        }

        const logStatus = (data.container_status || data.status || "").toLowerCase();

        if (["running", "restarting", "started"].includes(logStatus)) {
          setLogText("[패널] 서버 로그 수신을 기다리는 중입니다...");
          return;
        }

        setLogText("서버 로그가 없습니다.");
      } catch (err) {
        setLogText("서버 로그 조회 실패: " + err);
      }
    }

    async function loadServerStatus() {
      try {
        const response = await fetch("/api/server/status");
        const data = await response.json();
        const status = data.status || "error";
        document.getElementById("serverStatus").innerText = displayServerStatus(status);
        updateServerStatusIcon(status);
        setConfigLocked(isRunningStatus(status));
        setFileExplorerWriteLocked(isRunningStatus(status));
        return status;
      } catch (err) {
        document.getElementById("serverStatus").innerText = displayServerStatus("error");
        updateServerStatusIcon("error");
        setConfigLocked(false);
        setFileExplorerWriteLocked(false);
        return "error";
      }
    }

    async function loadStatus() {
      try {
        const response = await fetch("/api/install/status");
        const data = await response.json();
        document.getElementById("installStatus").innerText = displayInstallStatus(data.status);
        updateInstallStatusIcon(data.status);
      } catch (err) {
        document.getElementById("installStatus").innerText = displayInstallStatus("error");
        updateInstallStatusIcon("error");
      }
    }

    async function loadLog() {
      currentLogMode = "install";
      await loadInstallLogOnly();
    }

    async function loadInstallLogOnly() {
      try {
        const response = await fetch("/api/install/log");
        const data = await response.json();

        setLogText(data.log || "로그가 없습니다.");
      } catch (err) {
        setLogText("로그 조회 실패: " + err);
      }
    }

    async function refreshCurrentLog() {
      if (currentLogMode === "server") {
        await loadServerLog();
      } else {
        await loadInstallLogOnly();
      }
    }

    async function logout() {
      try {
        await fetch("/api/auth/logout", {
          method: "POST"
        });
      } catch (err) {
        // ignore
      }

      window.location.href = "/login";
    }

    async function initializeDashboard() {
      await loadStatus();
      const serverStatus = await loadServerStatus();
      const shouldShowServerLog = (serverStatus || "").toLowerCase() === "running";

      currentLogMode = shouldShowServerLog ? "server" : "install";
      await loadConfig();
      await loadRestartSchedule(true);

      if (shouldShowServerLog) {
        await loadServerLog();
      } else {
        await loadLog();
      }
    }

    setInterval(loadStatus, 2000);
    setInterval(loadServerStatus, 2000);
    setInterval(refreshCurrentLog, 2000);
    setInterval(function () { loadRestartSchedule(false); }, 30000);

    initializeDashboard();
  </script>
</body>
</html>
"""
    return html.replace("__PANEL_VERSION__", PANEL_VERSION)


@app.post("/api/install")
def request_install(request: Request, background_tasks: BackgroundTasks):
    global INSTALL_JOB_ACTIVE

    require_auth(request)

    with INSTALL_JOB_LOCK:
        if INSTALL_JOB_ACTIVE:
            return {
                "status": "running",
                "message": "이미 설치 작업이 실행 중입니다.",
            }

        INSTALL_JOB_ACTIVE = True

    background_tasks.add_task(install_palworld_job)

    return {
        "status": "started",
        "message": "설치 시작",
    }


@app.get("/api/install/status")
def install_status(request: Request):
    require_auth(request)

    return {
        "status": get_effective_install_status(),
    }


@app.get("/api/install/log")
def install_log(request: Request):
    require_auth(request)

    if not INSTALL_LOG_FILE.exists():
        return {
            "status": "empty",
            "log": "",
        }

    return {
        "status": get_status(),
        "log": sanitize_log_text(INSTALL_LOG_FILE.read_text(encoding="utf-8")),
    }


@app.get("/api/docker/status")
def docker_status(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()
        containers = client.containers.list(all=True)

        return {
            "status": "ok",
            "message": "Docker Engine 연결 성공",
            "containers": [
                {
                    "name": c.name,
                    "status": c.status,
                    "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                }
                for c in containers
            ],
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Docker Engine 연결 실패",
            "error": str(e),
        }


@app.get("/api/config")
def get_config(request: Request):
    require_auth(request)

    return {
        "status": "ok",
        "path": str(get_config_path()),
        "exists": get_config_path().exists(),
        "config": read_config(),
    }


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

    try:
        with RESTART_SCHEDULE_LOCK:
            schedule = load_restart_schedule()
            previous_enabled = schedule.get("enabled", False)
            previous_time = schedule.get("restart_time", "04:00")
            schedule["enabled"] = payload.enabled
            schedule["restart_time"] = normalize_restart_time(payload.restart_time)

            if schedule["enabled"] and (
                not previous_enabled or schedule["restart_time"] != previous_time
            ):
                schedule["last_run_date"] = ""

            saved = persist_restart_schedule(schedule)

        state = "활성화" if saved["enabled"] else "비활성화"
        return {
            "status": "ok",
            "message": f"게임 서버 자동 재시작이 {state}되었습니다.",
            "schedule": restart_schedule_response(saved),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/api/worlds")
def get_worlds(request: Request):
    require_auth(request)
    config = read_config()

    return {
        "status": "ok",
        "path": str(SAVED_WORLDS_DIR),
        "worlds": list_world_options(config),
        "saved_games": list_saved_world_names(),
        "selected_world": "",
    }


@app.post("/api/config")
def save_config(payload: ConfigRequest, request: Request):
    require_auth(request)

    if is_server_container_running():
        raise HTTPException(
            status_code=409,
            detail="서버 실행 중에는 설정을 변경할 수 없습니다. 서버를 중지한 뒤 다시 시도해주세요.",
        )

    try:
        payload_data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        config = normalize_config(payload_data)
        validation_error = validate_world_start_config(config)

        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        config_path = write_config(config)

        return {
            "status": "ok",
            "message": "PalWorldSettings.ini 저장 완료",
            "path": str(config_path),
            "config": config,
            "worlds": list_world_options(config),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"PalWorldSettings.ini 저장 중 오류가 발생했습니다: {e}",
        )


@app.post("/api/server/start")
def start_server(request: Request):
    require_auth(request)

    try:
        if get_effective_install_status() != "completed":
            return {
                "status": "error",
                "message": "공식 Palworld 서버 이미지가 설치되지 않았습니다. 먼저 엔진 설치를 진행해주세요.",
            }

        ensure_official_runtime_files()
        config_path = create_default_config()
        server_config = read_config()
        advanced_options = server_config.get("AdvancedOptions") or {}
        clear_server_control_log()
        write_server_control_log("Palworld 서버 시작 요청을 받았습니다.")

        validation_error = validate_world_start_config(server_config)

        if validation_error:
            write_server_control_log(f"서버 시작이 차단되었습니다. {validation_error}")

            return {
                "status": "config_error",
                "message": validation_error,
                "config": str(config_path),
                "worlds": list_world_options(server_config),
            }

        effective_server_port = int(server_config.get("PublicPort", SERVER_PORT))
        effective_rcon_port = int(server_config.get("RCONPort", RCON_PORT))
        effective_rest_port = int(advanced_options.get("RESTAPIPort", 8212))
        rest_api_enabled = bool(advanced_options.get("RESTAPIEnabled", False))
        client = docker.from_env()

        existing = client.containers.list(
            all=True,
            filters={"name": PALWORLD_SERVER_CONTAINER},
        )

        for container in existing:
            if container.name == PALWORLD_SERVER_CONTAINER:
                container.reload()

                if (
                    container.status == "running"
                    and server_container_keeps_stdin_open(container)
                    and server_container_uses_official_runtime(container)
                ):
                    return {
                        "status": "running",
                        "message": "Palworld 서버가 이미 실행 중입니다.",
                    }

                container.remove(force=True)

        try:
            client.images.get(PALWORLD_RUNTIME_IMAGE)
        except docker.errors.ImageNotFound:
            write_server_control_log("공식 Palworld 서버 이미지가 없어 시작을 중단했습니다.")
            return {
                "status": "install_required",
                "message": "공식 Palworld 1.0 이미지가 없습니다. 엔진 설치를 먼저 진행해주세요.",
            }

        ports = {
            f"{effective_server_port}/udp": effective_server_port,
        }

        if server_config.get("RCONEnabled"):
            ports[f"{effective_rcon_port}/tcp"] = effective_rcon_port

        if rest_api_enabled:
            ports[f"{effective_rest_port}/tcp"] = effective_rest_port

        host_saved_root = HOST_DATA_DIR / "server" / "Pal" / "Saved"

        container = client.containers.run(
            PALWORLD_RUNTIME_IMAGE,
            entrypoint=["/pal/helper.sh"],
            command=[
                f"-port={effective_server_port}",
                "-useperfthreads",
                "-NoAsyncLoadingThread",
                "-UseMultithreadForDS",
            ],
            name=PALWORLD_SERVER_CONTAINER,
            working_dir="/pal/Package",
            detach=True,
            stdin_open=True,
            restart_policy={"Name": "unless-stopped"},
            volumes={
                str(host_saved_root): {
                    "bind": "/pal/Package/Pal/Saved",
                    "mode": "rw",
                },
                str(HOST_RUNTIME_HELPER_FILE): {
                    "bind": "/pal/helper.sh",
                    "mode": "ro",
                },
            },
            ports=ports,
        )

        return {
            "status": "started",
            "message": "Palworld 서버 컨테이너를 시작했습니다.",
            "container": container.name,
            "config": str(config_path),
            "port": f"{effective_server_port}/udp",
            "rcon_port": f"{effective_rcon_port}/tcp" if server_config.get("RCONEnabled") else "",
            "rest_api_port": f"{effective_rest_port}/tcp" if rest_api_enabled else "",
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Palworld 서버 시작 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.post("/api/server/stop")
def stop_server(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        try:
            container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            return {
                "status": "not_created",
                "message": "Palworld 서버 컨테이너가 아직 생성되지 않았습니다.",
            }

        container.reload()
        write_server_control_log("Palworld 서버 중지 요청을 받았습니다.")

        try:
            container.update(restart_policy={"Name": "no"})
            write_server_control_log("Docker 재시작 정책을 해제했습니다.")
        except Exception as e:
            write_server_control_log(f"Docker 재시작 정책 해제 중 경고: {e}")

        container.reload()

        if container.status not in {"running", "restarting", "paused"}:
            write_server_control_log("Palworld 서버가 이미 종료된 상태입니다.")
            return {
                "status": "stopped",
                "message": "Palworld 서버가 이미 종료되어 있습니다.",
                "container": container.name,
                "log": combined_server_log(container, tail=80),
            }

        try:
            write_server_control_log(f"정상 종료 신호를 보냈습니다. 최대 {SERVER_STOP_GRACE_SECONDS}초만 대기합니다.")
            container.stop(timeout=SERVER_STOP_GRACE_SECONDS)
        except Exception as e:
            write_server_control_log(f"정상 중지 대기 중 경고가 발생해 강제 종료를 진행합니다: {e}")
            container.remove(force=True)
            write_server_control_log("Palworld 서버가 강제로 종료되었습니다.")

            return {
                "status": "stopped",
                "message": "Palworld 서버가 종료되었습니다.",
                "container": PALWORLD_SERVER_CONTAINER,
                "log": combined_server_log(),
            }

        try:
            container.reload()
        except Exception as reload_error:
            write_server_control_log(f"중지 후 컨테이너 상태 확인 중 경고: {reload_error}")
            write_server_control_log("Palworld 서버가 종료되었습니다.")

            return {
                "status": "stopped",
                "message": "Palworld 서버가 종료되었습니다.",
                "container": PALWORLD_SERVER_CONTAINER,
                "log": combined_server_log(tail=80),
            }

        if container.status in {"running", "restarting"}:
            write_server_control_log("짧은 대기 후에도 서버가 실행 중이라 강제 제거를 진행했습니다.")
            container.remove(force=True)
            write_server_control_log("Palworld 서버가 종료되었습니다.")

            return {
                "status": "stopped",
                "message": "Palworld 서버가 종료되었습니다.",
                "container": PALWORLD_SERVER_CONTAINER,
                "log": combined_server_log(),
            }

        write_server_control_log("Palworld 서버가 종료되었습니다.")

        return {
            "status": "stopped",
            "message": "Palworld 서버가 종료되었습니다.",
            "container": container.name,
            "log": combined_server_log(container, tail=80),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Palworld 서버 중지 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.post("/api/server/restart")
def restart_server(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        try:
            container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            return {
                "status": "not_created",
                "message": "Palworld 서버 컨테이너가 없습니다. 먼저 서버 시작을 눌러주세요.",
            }

        container.reload()

        if (
            not server_container_keeps_stdin_open(container)
            or not server_container_uses_official_runtime(container)
        ):
            container.remove(force=True)
            return start_server(request)

        if container.status == "running":
            container.restart(timeout=15)

            return {
                "status": "restarted",
                "message": "Palworld 서버를 재시작했습니다.",
                "container": container.name,
            }

        container.remove(force=True)
        return start_server(request)

    except Exception as e:
        return {
            "status": "error",
            "message": "Palworld 서버 재시작 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.get("/api/server/status")
def server_status(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        containers = client.containers.list(
            all=True,
            filters={"name": PALWORLD_SERVER_CONTAINER},
        )

        for container in containers:
            if container.name == PALWORLD_SERVER_CONTAINER:
                container.reload()

                if not server_container_uses_official_runtime(container):
                    return {
                        "status": "outdated",
                        "container": container.name,
                        "message": "이전 Palworld 런타임 컨테이너입니다. 서버 시작 또는 재시작을 누르면 공식 1.0 이미지로 교체됩니다.",
                    }

                return {
                    "status": container.status,
                    "container": container.name,
                    "image": container.image.tags[0] if container.image.tags else container.image.short_id,
                }

        return {
            "status": "not_created",
            "message": "Palworld 서버 컨테이너가 아직 생성되지 않았습니다.",
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }


@app.get("/api/server/log")
def server_log(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()
        try:
            container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            return {
                "status": "not_created",
                "log": combined_server_log(),
            }

        container.reload()
        include_control_log = container.status not in {"running", "restarting"}

        return {
            "status": container.status,
            "api_status": "ok",
            "container_status": container.status,
            "container": container.name,
            "log": combined_server_log(container, include_control_log=include_control_log),
        }

    except Exception as e:
        control_log = combined_server_log()

        return {
            "status": "error",
            "log": control_log,
            "error": str(e),
        }


@app.get("/api/files")
def list_files(request: Request, path: str = ""):
    require_auth(request)

    target = resolve_saved_path(path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="경로를 찾을 수 없습니다.")

    if not target.is_dir():
        raise HTTPException(status_code=400, detail="폴더 경로만 열 수 있습니다.")

    entries = []

    for child in target.iterdir():
        try:
            entries.append(file_entry(child))
        except OSError:
            continue

    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))

    return {
        "status": "ok",
        "root": "/Saved",
        "path": saved_relative_path(target),
        "parent": saved_relative_path(target.parent) if target.resolve() != saved_root() else "",
        "write_locked": is_server_container_running(),
        "entries": entries,
    }


@app.get("/api/files/download")
def download_file(request: Request, path: str):
    require_auth(request)

    target = resolve_saved_path(path)

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="다운로드할 파일을 찾을 수 없습니다.")

    return FileResponse(
        path=str(target),
        filename=target.name,
    )


@app.post("/api/files/upload")
async def upload_file(request: Request, path: str = "", file: UploadFile = File(...)):
    require_auth(request)
    require_server_stopped_for_file_write()

    target_dir = resolve_saved_path(path)

    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="업로드할 폴더를 찾을 수 없습니다.")

    filename = validate_entry_name(file.filename or f"upload-{int(time.time())}")
    target = (target_dir / filename).resolve()
    resolve_saved_path(saved_relative_path(target))

    with target.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "status": "ok",
        "message": "파일 업로드가 완료되었습니다.",
        "path": saved_relative_path(target),
        "filename": filename,
    }


@app.post("/api/files/mkdir")
def create_directory(payload: FileExplorerCreateDirRequest, request: Request):
    require_auth(request)
    require_server_stopped_for_file_write()

    parent = resolve_saved_path(payload.path)

    if not parent.exists() or not parent.is_dir():
        raise HTTPException(status_code=404, detail="폴더를 생성할 위치를 찾을 수 없습니다.")

    dirname = validate_entry_name(payload.name)
    target = (parent / dirname).resolve()
    resolve_saved_path(saved_relative_path(target))
    target.mkdir(parents=False, exist_ok=False)

    return {
        "status": "ok",
        "message": "폴더가 생성되었습니다.",
        "path": saved_relative_path(target),
    }


@app.post("/api/files/delete")
def delete_file_entry(payload: FileExplorerDeleteRequest, request: Request):
    require_auth(request)
    require_server_stopped_for_file_write()

    target = resolve_saved_path(payload.path)

    if target == saved_root():
        raise HTTPException(status_code=400, detail="Saved 루트 폴더는 삭제할 수 없습니다.")

    if not target.exists():
        raise HTTPException(status_code=404, detail="삭제할 항목을 찾을 수 없습니다.")

    if target.is_dir():
        try:
            target.rmdir()
        except OSError:
            raise HTTPException(status_code=400, detail="비어 있지 않은 폴더는 삭제할 수 없습니다.")
    else:
        target.unlink()

    return {
        "status": "ok",
        "message": "삭제되었습니다.",
        "path": payload.path,
    }


@app.get("/api/saves/download")
def download_saves(request: Request):
    require_auth(request)

    try:
        ensure_data_dirs()
        SAVED_WORLDS_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = SAVE_EXPORT_DIR / f"palworld-saves-{timestamp}.zip"
        zip_base = zip_path.with_suffix("")

        shutil.make_archive(
            base_name=str(zip_base),
            format="zip",
            root_dir=str(SAVED_WORLDS_DIR),
        )

        return FileResponse(
            path=str(zip_path),
            filename=zip_path.name,
            media_type="application/zip",
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"세이브 다운로드 중 오류가 발생했습니다: {e}",
        )


@app.post("/api/saves/upload")
async def upload_saves(request: Request, file: UploadFile = File(...)):
    require_auth(request)

    try:
        ensure_data_dirs()
        SAVED_WORLDS_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

        filename = Path(file.filename or f"uploaded-saves-{int(time.time())}.zip").name
        upload_path = SAVE_EXPORT_DIR / filename

        with upload_path.open("wb") as buffer:
            content = await file.read()
            buffer.write(content)

        if not zipfile.is_zipfile(upload_path):
            return {
                "status": "error",
                "message": "ZIP 파일만 업로드할 수 있습니다.",
                "filename": filename,
            }

        safe_extract_zip(upload_path, SAVED_WORLDS_DIR)

        return {
            "status": "ok",
            "message": "세이브 파일 업로드가 완료되었습니다.",
            "filename": filename,
            "target": str(SAVED_WORLDS_DIR),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "세이브 업로드 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "game": GAME_CODE,
        "version": PANEL_VERSION,
    }
