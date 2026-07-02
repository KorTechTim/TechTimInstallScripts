from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from datetime import datetime
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import zipfile

import docker

app = FastAPI(title="TechTim Palworld Server Panel")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

GAME_CODE = os.getenv("GAME_CODE", "palworld")
PANEL_VERSION = os.getenv("PANEL_VERSION", "1.0.0")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/palworld/data"))

STEAMCMD_IMAGE = os.getenv("STEAMCMD_IMAGE", "steamcmd/steamcmd:ubuntu")
PALWORLD_APP_ID = os.getenv("PALWORLD_APP_ID", "2394010")

PALWORLD_SERVER_CONTAINER = os.getenv("PALWORLD_SERVER_CONTAINER", "palworld-server")
PALWORLD_RUNTIME_IMAGE = os.getenv("PALWORLD_RUNTIME_IMAGE", STEAMCMD_IMAGE)
SERVER_PORT = int(os.getenv("SERVER_PORT", "8211"))
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))

INSTALL_REQUEST_FILE = DATA_DIR / "install-request.txt"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"
SERVER_CONTROL_LOG_FILE = DATA_DIR / "server-control.log"

SAVED_WORLDS_DIR = DATA_DIR / "server" / "Pal" / "Saved" / "SaveGames"
SAVE_EXPORT_DIR = DATA_DIR / "uploads"

AUTH_FILE = DATA_DIR / "auth.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SESSION_COOKIE_NAME = "techtim_session"


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


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "server").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "backups").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    SAVED_WORLDS_DIR.mkdir(parents=True, exist_ok=True)


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


def write_log(message: str) -> None:
    ensure_data_dirs()
    now = datetime.now().isoformat(timespec="seconds")

    with INSTALL_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")


def write_server_control_log(message: str) -> None:
    ensure_data_dirs()
    now = datetime.now().isoformat(timespec="seconds")

    with SERVER_CONTROL_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")


def clear_server_control_log() -> None:
    ensure_data_dirs()

    try:
        SERVER_CONTROL_LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass


def set_status(status: str) -> None:
    ensure_data_dirs()
    INSTALL_STATUS_FILE.write_text(status, encoding="utf-8")


def get_status() -> str:
    if not INSTALL_STATUS_FILE.exists():
        return "not_started"

    return INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip()


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
    "PalStomachDecreaceRate",
    "PalStaminaDecreaceRate",
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
    "EnablePredatorBossPal",
    "DropItemMaxNum",
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
    "bIsPvP",
    "bHardcore",
    "bPalLost",
    "bCanPickupOtherGuildDeathPenaltyDrop",
    "bEnableNonLoginPenalty",
    "bEnableFastTravel",
    "bExistPlayerAfterLogout",
    "bEnableDefenseOtherGuildPlayer",
    "bBuildAreaLimit",
    "ItemWeightRate",
    "bShowPlayerList",
    "CoopPlayerMaxNum",
    "RESTAPIEnabled",
    "RESTAPIPort",
    "bIsUseBackupSaveData",
    "Region",
    "bUseAuth",
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


def is_server_container_running() -> bool:
    try:
        client = docker.from_env()
        container = client.containers.get(PALWORLD_SERVER_CONTAINER)
        container.reload()
        return container.status == "running"
    except docker.errors.NotFound:
        return False
    except Exception:
        return False


def read_server_control_log() -> str:
    if not SERVER_CONTROL_LOG_FILE.exists():
        return ""

    try:
        return SERVER_CONTROL_LOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_container_log(container, tail: int = 200) -> str:
    return container.logs(
        stdout=True,
        stderr=True,
        tail=tail,
    ).decode("utf-8", errors="replace")


def combined_server_log(container=None, include_control_log: bool = True) -> str:
    logs = ""

    if container is not None:
        try:
            logs = read_container_log(container)
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


def install_palworld_job() -> None:
    ensure_data_dirs()

    INSTALL_LOG_FILE.write_text("", encoding="utf-8")
    set_status("running")

    write_log("Palworld Dedicated Server 설치 작업을 시작합니다.")
    write_log("SteamCMD anonymous 로그인을 사용합니다.")
    write_log("Steam 계정 정보 입력은 필요하지 않습니다.")
    write_log(f"Palworld Dedicated Server App ID: {PALWORLD_APP_ID}")

    try:
        server_dir = DATA_DIR / "server"
        host_server_dir = HOST_DATA_DIR / "server"
        host_server_dir.mkdir(parents=True, exist_ok=True)

        write_log(f"패널 내부 서버 경로: {server_dir}")
        write_log(f"호스트 서버 경로: {host_server_dir}")
        write_log(f"SteamCMD 이미지: {STEAMCMD_IMAGE}")

        client = docker.from_env()

        write_log("Docker Engine 연결 성공.")
        write_log("SteamCMD 이미지를 확인합니다. 최초 실행 시 pull 시간이 걸릴 수 있습니다.")
        client.images.pull(STEAMCMD_IMAGE)

        write_log("SteamCMD 이미지 준비 완료.")
        write_log("Palworld 서버 파일 다운로드를 시작합니다.")

        steamcmd_command = [
            "+force_install_dir", "/server",
            "+login", "anonymous",
            "+app_update", PALWORLD_APP_ID, "validate",
            "+quit",
        ]

        max_attempts = 3
        exit_code = -1

        for attempt in range(1, max_attempts + 1):
            container = None
            missing_configuration = False
            container_name = f"palworld-steamcmd-install-{int(time.time())}-{attempt}"

            write_log(f"SteamCMD 설치 시도 {attempt}/{max_attempts}")

            try:
                container = client.containers.run(
                    STEAMCMD_IMAGE,
                    command=steamcmd_command,
                    name=container_name,
                    detach=True,
                    remove=False,
                    volumes={
                        str(host_server_dir): {
                            "bind": "/server",
                            "mode": "rw",
                        }
                    },
                )

                for line in container.logs(stream=True, stdout=True, stderr=True, follow=True):
                    text = line.decode("utf-8", errors="replace").rstrip()

                    if "Missing configuration" in text:
                        missing_configuration = True

                    if text:
                        write_log(f"[steamcmd] {text}")

                result = container.wait()
                exit_code = result.get("StatusCode", -1)

            except Exception as attempt_error:
                exit_code = -1
                write_log(f"SteamCMD 설치 시도 중 오류 발생: {attempt_error}")

            finally:
                if container is not None:
                    try:
                        container.remove(force=True)
                    except Exception as remove_error:
                        write_log(f"SteamCMD 설치 컨테이너 삭제 중 경고: {remove_error}")

            if exit_code == 0:
                write_log(f"SteamCMD 설치 시도 {attempt}/{max_attempts} 성공")
                break

            write_log(f"WARNING: SteamCMD 설치 시도 {attempt}/{max_attempts} 실패. exit_code={exit_code}")

            if missing_configuration:
                write_log("Steam 서버에서 설치 정보를 일시적으로 받지 못했습니다. 잠시 후 자동 재시도합니다.")

            if attempt < max_attempts:
                time.sleep(20)

        if exit_code != 0:
            write_log(f"ERROR: SteamCMD 설치가 {max_attempts}회 모두 실패했습니다. 마지막 exit_code={exit_code}")
            write_log("App ID, anonymous 설치 지원 여부, Steam 서버 상태, 네트워크 상태를 확인해주세요.")
            set_status("failed")
            return

        server_executable = server_dir / "PalServer.sh"

        if not server_executable.exists():
            write_log("ERROR: 설치 명령은 종료되었지만 PalServer.sh 파일을 찾을 수 없습니다.")
            set_status("failed")
            return

        try:
            server_executable.chmod(0o755)
        except OSError as chmod_error:
            write_log(f"WARNING: PalServer.sh 실행 권한 설정 중 경고: {chmod_error}")

        create_default_config()

        INSTALL_REQUEST_FILE.write_text(
            "TechTim Palworld Dedicated Server install completed.\n"
            f"game={GAME_CODE}\n"
            f"panel_version={PANEL_VERSION}\n"
            "steam_login=anonymous\n"
            f"app_id={PALWORLD_APP_ID}\n"
            f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )

        write_log("Palworld Dedicated Server 파일 다운로드가 완료되었습니다.")
        write_log("이제 Web GUI에서 PalWorldSettings.ini를 저장하고 서버를 시작할 수 있습니다.")
        set_status("completed")

    except Exception as e:
        write_log(f"ERROR: 설치 작업 중 예외 발생: {e}")
        set_status("failed")


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
    .config h2 { margin: 0 0 16px; font-size: 22px; }
    .config-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    .advanced-card { margin-top: 18px; min-height: 112px; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.55); background: linear-gradient(90deg, rgba(11, 32, 42, 0.78), rgba(27, 96, 77, 0.36)), url("/static/palworld-settings-bg.png") center / cover no-repeat; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 18px; color: #ffffff; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.08); }
    .advanced-copy { min-width: 0; }
    .advanced-title { font-size: 20px; font-weight: bold; margin-bottom: 6px; }
    .advanced-subtitle { color: rgba(255,255,255,0.82); font-size: 13px; line-height: 1.45; }
    .advanced-button { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; width: 58px; height: 58px; border-radius: 14px; padding: 0; background: rgba(255,255,255,0.9); color: #0f766e; box-shadow: 0 14px 32px rgba(0,0,0,0.24); }
    .advanced-button img { width: 38px; height: 38px; display: block; object-fit: cover; border-radius: 10px; }
    .modal-backdrop { position: fixed; inset: 0; z-index: 100; display: none; align-items: center; justify-content: center; padding: 24px; background: rgba(10, 18, 28, 0.62); }
    .modal-backdrop.show { display: flex; }
    .modal { width: min(1060px, 100%); max-height: min(86vh, 900px); overflow: hidden; border-radius: 16px; border: 1px solid rgba(255,255,255,0.45); background: rgba(255,255,255,0.96); box-shadow: 0 28px 90px rgba(0,0,0,0.45); display: flex; flex-direction: column; }
    .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 20px; background: linear-gradient(90deg, rgba(34, 148, 177, 0.18), rgba(73, 182, 122, 0.16)); border-bottom: 1px solid #e5e7eb; }
    .modal-head h2 { margin: 0; font-size: 22px; }
    .modal-close { width: 40px; height: 40px; border-radius: 50%; padding: 0; background: #111827; color: #fff; }
    .modal-body { overflow: auto; padding: 20px; }
    .advanced-group { margin-bottom: 22px; }
    .advanced-group h3 { margin: 0 0 12px; font-size: 17px; color: #111827; }
    .advanced-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .advanced-check { margin-top: 0; align-self: end; min-height: 42px; }
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
      .advanced-card { align-items: flex-start; }
      .advanced-grid { grid-template-columns: 1fr; }
      button { width: 100%; }
      .advanced-button, .modal-close { width: 44px; height: 44px; }
      .config-save-wrap { width: 100%; }
      .save-bubble { max-width: calc(100% - 24px); white-space: normal; }
      .help::after { right: auto; left: 50%; transform: translate(-50%, 4px); max-width: min(220px, calc(100vw - 48px)); }
      .help:hover::after, .help:focus::after { transform: translate(-50%, 0); }
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
        <div class="advanced-card">
          <div class="advanced-copy">
            <div class="advanced-title">Palworld 상세 서버 설정</div>
            <div class="advanced-subtitle">경험치, 포획률, 낮/밤 속도, 알 부화 시간, 전투 배율, 월드 규칙을 팝업에서 조정합니다.</div>
          </div>
          <button id="advancedSettingsBtn" class="advanced-button" type="button" onclick="openAdvancedSettings()" title="상세 설정 열기" aria-label="상세 설정 열기">
            <img src="/static/palworld-settings-icon.png" alt="">
          </button>
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
          <button id="advancedSaveBtn" type="button" onclick="saveConfig()">설정 저장</button>
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
      <button class="secondary" onclick="downloadSaves()">세이브 다운로드</button>
      <button class="secondary" onclick="triggerSaveUpload()">세이브 업로드</button>
      <input id="saveUploadInput" type="file" accept=".zip" onchange="uploadSaves()" style="display:none">
    </div>

    <div id="result" class="result" hidden></div>

  </div>

  <script>
    let currentLogMode = "install";
    let advancedOptions = {};

    const advancedOptionGroups = [
      {
        title: "배율",
        fields: [
          { key: "DayTimeSpeedRate", label: "낮 시간 속도", type: "number", step: "0.1", min: "0.1" },
          { key: "NightTimeSpeedRate", label: "밤 시간 속도", type: "number", step: "0.1", min: "0.1" },
          { key: "ExpRate", label: "경험치 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "PalCaptureRate", label: "포획률", type: "number", step: "0.1", min: "0.1" },
          { key: "PalSpawnNumRate", label: "팰 출현 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "CollectionDropRate", label: "채집 드롭 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "EnemyDropItemRate", label: "적 드롭 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "WorkSpeedRate", label: "작업 속도", type: "number", step: "0.1", min: "0.1" },
          { key: "PalEggDefaultHatchingTime", label: "알 부화 시간", type: "number", step: "0.1", min: "0" }
        ]
      },
      {
        title: "전투",
        fields: [
          { key: "PlayerDamageRateAttack", label: "플레이어 공격 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "PlayerDamageRateDefense", label: "플레이어 방어 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "PalDamageRateAttack", label: "팰 공격 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "PalDamageRateDefense", label: "팰 방어 배율", type: "number", step: "0.1", min: "0.1" },
          { key: "DeathPenalty", label: "사망 패널티", type: "select", options: ["None", "Item", "ItemAndEquipment", "All"] },
          { key: "bEnablePlayerToPlayerDamage", label: "플레이어 간 피해", type: "checkbox" },
          { key: "bEnableFriendlyFire", label: "아군 피해", type: "checkbox" },
          { key: "bIsPvP", label: "PvP 모드", type: "checkbox" },
          { key: "bHardcore", label: "하드코어", type: "checkbox" }
        ]
      },
      {
        title: "생존/월드",
        fields: [
          { key: "PlayerStomachDecreaceRate", label: "플레이어 포만감 감소", type: "number", step: "0.1", min: "0" },
          { key: "PlayerStaminaDecreaceRate", label: "플레이어 스태미나 감소", type: "number", step: "0.1", min: "0" },
          { key: "PalStomachDecreaceRate", label: "팰 포만감 감소", type: "number", step: "0.1", min: "0" },
          { key: "PalStaminaDecreaceRate", label: "팰 스태미나 감소", type: "number", step: "0.1", min: "0" },
          { key: "ItemWeightRate", label: "아이템 무게 배율", type: "number", step: "0.1", min: "0" },
          { key: "bEnableFastTravel", label: "빠른 이동 허용", type: "checkbox" },
          { key: "bEnableFastTravelOnlyBaseCamp", label: "거점 빠른 이동만 허용", type: "checkbox" },
          { key: "EnablePredatorBossPal", label: "프레데터 보스 팰", type: "checkbox" },
          { key: "bPalLost", label: "팰 손실", type: "checkbox" }
        ]
      },
      {
        title: "거점/길드",
        fields: [
          { key: "BaseCampMaxNum", label: "전체 거점 최대 수", type: "number", step: "1", min: "1" },
          { key: "BaseCampMaxNumInGuild", label: "길드 거점 최대 수", type: "number", step: "1", min: "1" },
          { key: "BaseCampWorkerMaxNum", label: "거점 작업 팰 수", type: "number", step: "1", min: "1" },
          { key: "GuildPlayerMaxNum", label: "길드 최대 인원", type: "number", step: "1", min: "1" },
          { key: "bAutoResetGuildNoOnlinePlayers", label: "미접속 길드 자동 초기화", type: "checkbox" },
          { key: "AutoResetGuildTimeNoOnlinePlayers", label: "길드 초기화 시간", type: "number", step: "1", min: "1" },
          { key: "bAllowGlobalPalboxExport", label: "글로벌 팰박스 내보내기", type: "checkbox" },
          { key: "bAllowGlobalPalboxImport", label: "글로벌 팰박스 가져오기", type: "checkbox" }
        ]
      },
      {
        title: "운영",
        fields: [
          { key: "AutoSaveSpan", label: "자동 저장 간격", type: "number", step: "1", min: "1" },
          { key: "SupplyDropSpan", label: "보급품 드롭 간격", type: "number", step: "1", min: "0" },
          { key: "ChatPostLimitPerMinute", label: "분당 채팅 제한", type: "number", step: "1", min: "1" },
          { key: "DropItemMaxNum", label: "드롭 아이템 최대 수", type: "number", step: "1", min: "0" },
          { key: "DropItemAliveMaxHours", label: "드롭 아이템 유지 시간", type: "number", step: "0.1", min: "0" },
          { key: "ServerReplicatePawnCullDistance", label: "서버 복제 거리", type: "number", step: "100", min: "1000" },
          { key: "bShowPlayerList", label: "플레이어 목록 표시", type: "checkbox" },
          { key: "bIsShowJoinLeftMessage", label: "입장/퇴장 메시지", type: "checkbox" },
          { key: "bAllowClientMod", label: "클라이언트 모드 허용", type: "checkbox" }
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
      } else if (["not_started", "failed", "error"].includes(normalized)) {
        setStatusIcon("installStatusIcon", "status-bad", "x");
      } else {
        setStatusIcon("installStatusIcon", "status-pending", "pending");
      }
    }

    function updateServerStatusIcon(status) {
      const normalized = (status || "").toLowerCase();

      if (normalized === "running") {
        setStatusIcon("serverStatusIcon", "server-live", "server");
      } else if (["not_created", "stopped", "exited", "dead", "error", "config_error"].includes(normalized)) {
        setStatusIcon("serverStatusIcon", "status-bad", "x");
      } else {
        setStatusIcon("serverStatusIcon", "status-pending", "pending");
      }
    }

    function isRunningStatus(status) {
      return (status || "").toLowerCase() === "running";
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
        await loadServerLog();
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
        await loadServerLog();

      } catch (err) {
        result.innerText = "서버 시작 요청 실패: " + err;
      }
    }

    async function stopServer() {
      const result = document.getElementById("result");

      result.innerText = "Palworld 서버를 중지하는 중입니다...";

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

    function downloadSaves() {
      const result = document.getElementById("result");
      result.innerText = "세이브 파일 다운로드를 준비합니다...";
      window.location.href = "/api/saves/download";
    }

    function triggerSaveUpload() {
      const input = document.getElementById("saveUploadInput");
      input.click();
    }

    async function uploadSaves() {
      const result = document.getElementById("result");
      const input = document.getElementById("saveUploadInput");

      if (!input.files || input.files.length === 0) {
        result.innerText = "업로드할 세이브 ZIP 파일을 선택해주세요.";
        return;
      }

      const selectedFile = input.files[0];
      const formData = new FormData();
      formData.append("file", selectedFile);

      result.innerText = "세이브 파일을 업로드하는 중입니다...";

      try {
        const response = await fetch("/api/saves/upload", {
          method: "POST",
          body: formData
        });

        const data = await response.json();

        result.innerText =
          "세이브 업로드 결과\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + (data.message || "") + "\\n" +
          "파일명: " + (data.filename || selectedFile.name) + "\\n" +
          (data.target ? "저장 위치: " + data.target : "");

      } catch (err) {
        result.innerText = "세이브 업로드 실패: " + err;
      } finally {
        input.value = "";
      }
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
            } else {
              input = document.createElement("input");
              input.type = field.type || "text";
              if (field.step !== undefined) input.step = field.step;
              if (field.min !== undefined) input.min = field.min;
            }

            input.id = "adv_" + field.key;
            input.dataset.advancedKey = field.key;
            input.dataset.advancedType = field.type || "text";
            wrapper.appendChild(input);
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
      });
    }

    function readAdvancedOptions() {
      const options = Object.assign({}, advancedOptions);

      document.querySelectorAll("[data-advanced-key]").forEach(function (element) {
        const key = element.dataset.advancedKey;

        if (element.dataset.advancedType === "checkbox") {
          options[key] = element.checked;
        } else if (element.dataset.advancedType === "number") {
          options[key] = Number(element.value || 0);
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

    function showConfigSaveBubble() {
      const bubble = document.getElementById("configSaveBubble");

      if (!bubble) {
        return;
      }

      window.clearTimeout(configSaveBubbleTimer);
      bubble.classList.add("show");

      configSaveBubbleTimer = window.setTimeout(function () {
        bubble.classList.remove("show");
      }, 2200);
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

    async function saveConfig() {
      const result = document.getElementById("result");

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
        showConfigSaveBubble();
        await loadServerStatus();
        result.innerText = "설정 저장 완료\\n저장 위치: " + data.path;
      } catch (err) {
        result.innerText = "설정 저장 실패: " + err;
      }
    }

    async function loadServerLog() {
      currentLogMode = "server";

      try {
        const response = await fetch("/api/server/log");
        const data = await response.json();

        setLogText(data.log || data.error || "서버 로그가 없습니다.");
      } catch (err) {
        setLogText("서버 로그 조회 실패: " + err);
      }
    }

    async function loadServerStatus() {
      try {
        const response = await fetch("/api/server/status");
        const data = await response.json();
        const status = data.status || "error";
        document.getElementById("serverStatus").innerText = status;
        updateServerStatusIcon(status);
        setConfigLocked(isRunningStatus(status));
        return status;
      } catch (err) {
        document.getElementById("serverStatus").innerText = "error";
        updateServerStatusIcon("error");
        setConfigLocked(false);
        return "error";
      }
    }

    async function loadStatus() {
      try {
        const response = await fetch("/api/install/status");
        const data = await response.json();
        document.getElementById("installStatus").innerText = data.status;
        updateInstallStatusIcon(data.status);
      } catch (err) {
        document.getElementById("installStatus").innerText = "error";
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

      if (shouldShowServerLog) {
        await loadServerLog();
      } else {
        await loadLog();
      }
    }

    setInterval(loadStatus, 2000);
    setInterval(loadServerStatus, 2000);
    setInterval(refreshCurrentLog, 2000);

    initializeDashboard();
  </script>
</body>
</html>
"""
    return html.replace("__PANEL_VERSION__", PANEL_VERSION)


@app.post("/api/install")
def request_install(request: Request, background_tasks: BackgroundTasks):
    require_auth(request)

    current_status = get_status()

    if current_status == "running":
        return {
            "status": "running",
            "message": "이미 설치 작업이 실행 중입니다.",
        }

    background_tasks.add_task(install_palworld_job)

    return {
        "status": "started",
        "message": "설치 시작",
    }


@app.get("/api/install/status")
def install_status(request: Request):
    require_auth(request)

    return {
        "status": get_status(),
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
        "log": INSTALL_LOG_FILE.read_text(encoding="utf-8"),
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
        server_dir = DATA_DIR / "server"
        host_server_dir = HOST_DATA_DIR / "server"

        if not server_dir.exists():
            return {
                "status": "error",
                "message": "서버 파일이 없습니다. 먼저 엔진 설치를 진행해주세요.",
            }

        server_executable = server_dir / "PalServer.sh"

        if not server_executable.exists():
            return {
                "status": "error",
                "message": "PalServer.sh 파일을 찾을 수 없습니다. Palworld 서버 파일 설치 상태를 확인해주세요.",
            }

        config_path = create_default_config()
        server_config = read_config()
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
        client = docker.from_env()

        existing = client.containers.list(
            all=True,
            filters={"name": PALWORLD_SERVER_CONTAINER},
        )

        for container in existing:
            if container.name == PALWORLD_SERVER_CONTAINER:
                container.reload()

                if container.status == "running" and server_container_keeps_stdin_open(container):
                    return {
                        "status": "running",
                        "message": "Palworld 서버가 이미 실행 중입니다.",
                    }

                container.remove(force=True)

        try:
            server_executable.chmod(0o755)
        except OSError:
            pass

        client.images.pull(PALWORLD_RUNTIME_IMAGE)

        ports = {
            f"{effective_server_port}/udp": effective_server_port,
        }

        if server_config.get("RCONEnabled"):
            ports[f"{effective_rcon_port}/tcp"] = effective_rcon_port

        container = client.containers.run(
            PALWORLD_RUNTIME_IMAGE,
            command=[
                "bash",
                "-lc",
                f"./PalServer.sh -port={effective_server_port} -useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS",
            ],
            name=PALWORLD_SERVER_CONTAINER,
            working_dir="/server",
            detach=True,
            stdin_open=True,
            restart_policy={"Name": "unless-stopped"},
            volumes={
                str(host_server_dir): {
                    "bind": "/server",
                    "mode": "rw",
                }
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
                "log": combined_server_log(container),
            }

        try:
            container.stop(timeout=30)
        except Exception as e:
            write_server_control_log(f"정상 중지 실패, 강제 종료를 시도합니다: {e}")
            container.remove(force=True)
            write_server_control_log("Palworld 서버가 강제로 종료되었습니다.")

            return {
                "status": "stopped",
                "message": "Palworld 서버가 종료되었습니다.",
                "container": PALWORLD_SERVER_CONTAINER,
                "log": combined_server_log(),
            }

        time.sleep(2)
        container.reload()

        if container.status in {"running", "restarting"}:
            write_server_control_log("중지 후 서버가 다시 실행되어 강제 제거를 진행했습니다.")
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
            "log": combined_server_log(container),
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

        if not server_container_keeps_stdin_open(container):
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
            "status": "ok",
            "log": combined_server_log(container, include_control_log=include_control_log),
        }

    except Exception as e:
        control_log = combined_server_log()

        return {
            "status": "error",
            "log": control_log,
            "error": str(e),
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
