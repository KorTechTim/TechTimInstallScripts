from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import secrets
import shutil
import time
import zipfile

import docker

app = FastAPI(title="TechTim Romestead Server Panel")

GAME_CODE = os.getenv("GAME_CODE", "romestead")
PANEL_VERSION = os.getenv("PANEL_VERSION", "0.2.0")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/romestead/data"))

STEAMCMD_IMAGE = os.getenv("STEAMCMD_IMAGE", "steamcmd/steamcmd:ubuntu")
ROMESTEAD_APP_ID = os.getenv("ROMESTEAD_APP_ID", "4763510")

ROMESTEAD_SERVER_CONTAINER = os.getenv("ROMESTEAD_SERVER_CONTAINER", "romestead-server")
DOTNET_IMAGE = os.getenv("DOTNET_IMAGE", "mcr.microsoft.com/dotnet/runtime:8.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8050"))

INSTALL_REQUEST_FILE = DATA_DIR / "install-request.txt"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"

SAVED_WORLDS_DIR = DATA_DIR / "server" / "saved_worlds"
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
    AutoStartWorldName: str = "world"
    AutoCreateAndLoadWorld: bool = True
    AutoCreateWorldSize: int = 1
    Password: str = ""
    Port: int = SERVER_PORT
    MaxPlayers: int = 10
    EnableCheats: bool = False


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


def set_status(status: str) -> None:
    ensure_data_dirs()
    INSTALL_STATUS_FILE.write_text(status, encoding="utf-8")


def get_status() -> str:
    if not INSTALL_STATUS_FILE.exists():
        return "not_started"

    return INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip()


def get_config_path() -> Path:
    return DATA_DIR / "server" / "config.json"


def default_config() -> dict:
    return {
        "AutoStartWorldName": "world",
        "AutoCreateAndLoadWorld": True,
        "AutoCreateWorldSize": 1,
        "Password": "",
        "Port": SERVER_PORT,
        "MaxPlayers": 10,
        "EnableCheats": False,
    }


def normalize_config(config: dict) -> dict:
    merged = default_config()
    merged.update(config)

    merged["AutoStartWorldName"] = str(merged.get("AutoStartWorldName") or "world").strip() or "world"
    merged["AutoCreateAndLoadWorld"] = bool(merged.get("AutoCreateAndLoadWorld"))
    merged["AutoCreateWorldSize"] = max(1, min(5, int(merged.get("AutoCreateWorldSize") or 1)))
    merged["Password"] = str(merged.get("Password") or "")
    merged["Port"] = max(1, min(65535, int(merged.get("Port") or SERVER_PORT)))
    merged["MaxPlayers"] = max(1, min(100, int(merged.get("MaxPlayers") or 10)))
    merged["EnableCheats"] = bool(merged.get("EnableCheats"))

    return merged


def read_config() -> dict:
    config_path = get_config_path()

    if not config_path.exists():
        return default_config()

    try:
        return normalize_config(json.loads(config_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError):
        return default_config()


def write_config(config: dict) -> Path:
    server_dir = DATA_DIR / "server"
    server_dir.mkdir(parents=True, exist_ok=True)

    config_path = get_config_path()
    normalized = normalize_config(config)

    config_path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return config_path


def create_default_config() -> Path:
    config_path = get_config_path()

    if config_path.exists():
        return config_path

    return write_config(default_config())


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = (target_dir / member).resolve()

            if member_path != target_root and not str(member_path).startswith(str(target_root) + os.sep):
                raise ValueError("ZIP 파일에 허용되지 않는 경로가 포함되어 있습니다.")

        zip_ref.extractall(target_dir)


def install_romestead_job() -> None:
    ensure_data_dirs()

    INSTALL_LOG_FILE.write_text("", encoding="utf-8")
    set_status("running")

    write_log("Romestead Dedicated Server 설치 작업을 시작합니다.")
    write_log("SteamCMD anonymous 로그인을 사용합니다.")
    write_log("Steam 계정 정보 입력은 필요하지 않습니다.")
    write_log(f"Romestead Dedicated Server App ID: {ROMESTEAD_APP_ID}")

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
        write_log("Romestead 서버 파일 다운로드를 시작합니다.")

        steamcmd_command = [
            "+force_install_dir", "/server",
            "+login", "anonymous",
            "+app_update", ROMESTEAD_APP_ID, "validate",
            "+quit",
        ]

        container_name = f"romestead-steamcmd-install-{int(time.time())}"

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

            if text:
                write_log(f"[steamcmd] {text}")

        result = container.wait()
        exit_code = result.get("StatusCode", -1)

        try:
            container.remove(force=True)
        except Exception as remove_error:
            write_log(f"SteamCMD 설치 컨테이너 삭제 중 경고: {remove_error}")

        if exit_code != 0:
            write_log(f"ERROR: SteamCMD 설치 컨테이너가 비정상 종료되었습니다. exit_code={exit_code}")
            write_log("App ID, anonymous 설치 지원 여부, 네트워크 상태를 확인해주세요.")
            set_status("failed")
            return

        server_dll = server_dir / "Server.dll"

        if not server_dll.exists():
            write_log("ERROR: 설치 명령은 종료되었지만 Server.dll 파일을 찾을 수 없습니다.")
            set_status("failed")
            return

        INSTALL_REQUEST_FILE.write_text(
            "TechTim Romestead Dedicated Server install completed.\n"
            f"game={GAME_CODE}\n"
            f"panel_version={PANEL_VERSION}\n"
            "steam_login=anonymous\n"
            f"app_id={ROMESTEAD_APP_ID}\n"
            f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )

        write_log("Romestead Dedicated Server 파일 다운로드가 완료되었습니다.")
        write_log("이제 Web GUI에서 config.json을 저장하고 서버를 시작할 수 있습니다.")
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
  <title>TechTim Romestead Login</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #f4f6f8; color: #1f2937; }
    .box { max-width: 420px; margin: 100px auto; background: #fff; border-radius: 16px; padding: 34px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); }
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
    <h1>TechTim Romestead Panel</h1>
    <p>관리자 계정으로 로그인하세요.</p>

    <label>아이디</label>
    <input id="username" type="text" value="admin" autocomplete="username">

    <label>비밀번호</label>
    <input id="password" type="password" placeholder="비밀번호" autocomplete="current-password">

    <button onclick="login()">로그인</button>

    <div class="hint">
      최초 기본 계정은 <b>admin / admin</b> 입니다.<br>
      첫 로그인 후 반드시 비밀번호를 변경해야 합니다.
    </div>

    <div id="error" class="error"></div>
  </div>

  <script>
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
    body { margin: 0; font-family: Arial, sans-serif; background: #f4f6f8; color: #1f2937; }
    .box { max-width: 460px; margin: 90px auto; background: #fff; border-radius: 16px; padding: 34px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); }
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

    <label>새 비밀번호</label>
    <input id="newPassword" type="password" autocomplete="new-password">

    <label>새 비밀번호 확인</label>
    <input id="confirmPassword" type="password" autocomplete="new-password">

    <button onclick="changePassword()">비밀번호 변경</button>

    <div id="error" class="error"></div>
  </div>

  <script>
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
  <title>TechTim Romestead Server Panel</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #f4f6f8; color: #1f2937; }
    .wrap { max-width: 1180px; margin: 40px auto; background: #ffffff; border-radius: 16px; padding: 40px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); }
    h1 { margin: 0; font-size: 34px; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 24px; }
    .card { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 12px; padding: 18px; }
    .label { font-size: 13px; color: #6b7280; margin-bottom: 8px; }
    .value { font-size: 20px; font-weight: bold; }
    .actions { margin-top: 24px; display: flex; gap: 12px; flex-wrap: wrap; }
    .config { margin-top: 24px; padding: 20px; border: 1px solid #e5e7eb; border-radius: 12px; background: #ffffff; }
    .config h2 { margin: 0 0 16px; font-size: 22px; }
    .config-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    label { display: block; font-size: 13px; font-weight: bold; color: #374151; }
    input, select { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 11px; border: 1px solid #d1d5db; border-radius: 10px; font-size: 14px; }
    .checkline { display: flex; align-items: center; gap: 10px; margin-top: 28px; }
    .checkline input { width: auto; margin: 0; }
    button { border: 0; border-radius: 10px; padding: 14px 20px; font-weight: bold; cursor: pointer; background: #2563eb; color: white; }
    button.secondary { background: #e5e7eb; color: #1f2937; }
    button.danger { background: #dc2626; color: white; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .result { margin-top: 24px; padding: 16px; border-radius: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #374151; min-height: 22px; white-space: pre-line; }
    .log { margin-top: 24px; background: #111827; color: #d1d5db; border-radius: 12px; padding: 18px; min-height: 320px; max-height: 500px; overflow: auto; font-family: Consolas, Monaco, monospace; font-size: 13px; white-space: pre-wrap; }
    .links { margin-top: 28px; display: flex; gap: 12px; flex-wrap: wrap; }
    .link-button { display: inline-flex; align-items: center; justify-content: center; border-radius: 10px; padding: 14px 20px; font-weight: bold; color: white; text-decoration: none; background: #5865f2; }
    .link-button.youtube { background: #dc2626; }
    @media (max-width: 900px) {
      .wrap { margin: 0; border-radius: 0; padding: 20px; }
      .grid, .config-grid { grid-template-columns: 1fr; }
      button, .link-button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>TechTim Romestead Server Panel</h1>

    <div class="grid">
      <div class="card">
        <div class="label">게임</div>
        <div class="value">Romestead</div>
      </div>
      <div class="card">
        <div class="label">설치 상태</div>
        <div id="installStatus" class="value">확인 중</div>
      </div>
      <div class="card">
        <div class="label">서버 상태</div>
        <div id="serverStatus" class="value">확인 중</div>
      </div>
      <div class="card">
        <div class="label">패널 버전</div>
        <div class="value">__PANEL_VERSION__</div>
      </div>
    </div>

    <div id="installLog" class="log">설치 로그가 여기에 표시됩니다.</div>

    <div class="config">
      <h2>서버 설정</h2>
      <div class="config-grid">
        <label>월드 이름
          <input id="cfgWorldName" type="text">
        </label>
        <label>월드 크기
          <input id="cfgWorldSize" type="number" min="1" max="5" step="1">
        </label>
        <label>서버 비밀번호
          <input id="cfgPassword" type="text">
        </label>
        <label>서버 포트
          <input id="cfgPort" type="number" min="1" max="65535" step="1">
        </label>
        <label>최대 인원
          <input id="cfgMaxPlayers" type="number" min="1" max="100" step="1">
        </label>
        <label class="checkline">
          <input id="cfgAutoCreate" type="checkbox">
          자동 월드 생성
        </label>
        <label class="checkline">
          <input id="cfgCheats" type="checkbox">
          치트 허용
        </label>
        <div>
          <button onclick="saveConfig()">설정 저장</button>
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
      <button class="danger" onclick="logout()">로그아웃</button>
    </div>

    <div id="result" class="result" hidden></div>

    <div class="links">
      <a class="link-button" href="https://discord.gg/Awy6Uh38KW" target="_blank" rel="noopener noreferrer">디스코드 접속</a>
      <a class="link-button youtube" href="https://www.youtube.com/@kortechtim" target="_blank" rel="noopener noreferrer">유튜브채널 접속</a>
    </div>
  </div>

  <script>
    let currentLogMode = "install";

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

    async function requestInstall() {
      const btn = document.getElementById("installBtn");
      const result = document.getElementById("result");

      currentLogMode = "install";
      btn.disabled = true;
      result.innerText = "Romestead 엔진 설치 작업을 시작하는 중입니다...";

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

      result.innerText = "Romestead 서버를 시작하는 중입니다...";

      try {
        const response = await fetch("/api/server/start", {
          method: "POST"
        });

        const data = await response.json();

        result.innerText =
          "서버 시작 요청 결과\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + (data.message || "") + "\\n" +
          (data.port ? "포트: " + data.port + "\\n" : "") +
          (data.container ? "컨테이너: " + data.container : "");

        currentLogMode = "server";
        await loadServerStatus();
        await loadServerLog();

      } catch (err) {
        result.innerText = "서버 시작 요청 실패: " + err;
      }
    }

    async function stopServer() {
      const result = document.getElementById("result");

      result.innerText = "Romestead 서버를 중지하는 중입니다...";

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
        await loadServerLog();

      } catch (err) {
        result.innerText = "서버 중지 요청 실패: " + err;
      }
    }

    async function restartServer() {
      const result = document.getElementById("result");

      result.innerText = "Romestead 서버를 재시작하는 중입니다...";

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

    function fillConfig(config) {
      document.getElementById("cfgWorldName").value = config.AutoStartWorldName || "world";
      document.getElementById("cfgWorldSize").value = config.AutoCreateWorldSize || 1;
      document.getElementById("cfgPassword").value = config.Password || "";
      document.getElementById("cfgPort").value = config.Port || 8050;
      document.getElementById("cfgMaxPlayers").value = config.MaxPlayers || 10;
      document.getElementById("cfgAutoCreate").checked = Boolean(config.AutoCreateAndLoadWorld);
      document.getElementById("cfgCheats").checked = Boolean(config.EnableCheats);
    }

    function readConfigForm() {
      return {
        AutoStartWorldName: document.getElementById("cfgWorldName").value.trim() || "world",
        AutoCreateAndLoadWorld: document.getElementById("cfgAutoCreate").checked,
        AutoCreateWorldSize: Number(document.getElementById("cfgWorldSize").value || 1),
        Password: document.getElementById("cfgPassword").value,
        Port: Number(document.getElementById("cfgPort").value || 8050),
        MaxPlayers: Number(document.getElementById("cfgMaxPlayers").value || 10),
        EnableCheats: document.getElementById("cfgCheats").checked
      };
    }

    async function loadConfig() {
      try {
        const response = await fetch("/api/config");
        const data = await response.json();
        fillConfig(data.config || {});
      } catch (err) {
        document.getElementById("result").innerText = "설정 불러오기 실패: " + err;
      }
    }

    async function saveConfig() {
      const result = document.getElementById("result");
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
        document.getElementById("serverStatus").innerText = data.status || "error";
      } catch (err) {
        document.getElementById("serverStatus").innerText = "error";
      }
    }

    async function loadStatus() {
      try {
        const response = await fetch("/api/install/status");
        const data = await response.json();
        document.getElementById("installStatus").innerText = data.status;
      } catch (err) {
        document.getElementById("installStatus").innerText = "error";
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

    setInterval(loadStatus, 2000);
    setInterval(loadServerStatus, 2000);
    setInterval(refreshCurrentLog, 2000);

    loadStatus();
    loadServerStatus();
    loadConfig();
    loadLog();
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

    background_tasks.add_task(install_romestead_job)

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


@app.post("/api/config")
def save_config(payload: ConfigRequest, request: Request):
    require_auth(request)

    try:
        payload_data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        config = normalize_config(payload_data)
        config_path = write_config(config)

        return {
            "status": "ok",
            "message": "config.json 저장 완료",
            "path": str(config_path),
            "config": config,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"config.json 저장 중 오류가 발생했습니다: {e}",
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

        server_dll = server_dir / "Server.dll"

        if not server_dll.exists():
            return {
                "status": "error",
                "message": "Server.dll 파일을 찾을 수 없습니다. Romestead 서버 파일 설치 상태를 확인해주세요.",
            }

        config_path = create_default_config()
        server_config = read_config()
        effective_server_port = int(server_config.get("Port", SERVER_PORT))
        client = docker.from_env()

        existing = client.containers.list(
            all=True,
            filters={"name": ROMESTEAD_SERVER_CONTAINER},
        )

        for container in existing:
            if container.name == ROMESTEAD_SERVER_CONTAINER:
                container.reload()

                if container.status == "running":
                    return {
                        "status": "running",
                        "message": "Romestead 서버가 이미 실행 중입니다.",
                    }

                container.remove(force=True)

        client.images.pull(DOTNET_IMAGE)

        container = client.containers.run(
            DOTNET_IMAGE,
            command=["dotnet", "Server.dll"],
            name=ROMESTEAD_SERVER_CONTAINER,
            working_dir="/server",
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            volumes={
                str(host_server_dir): {
                    "bind": "/server",
                    "mode": "rw",
                }
            },
            ports={
                f"{effective_server_port}/udp": effective_server_port,
            },
        )

        return {
            "status": "started",
            "message": "Romestead 서버 컨테이너를 시작했습니다.",
            "container": container.name,
            "config": str(config_path),
            "port": f"{effective_server_port}/udp",
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Romestead 서버 시작 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.post("/api/server/stop")
def stop_server(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        try:
            container = client.containers.get(ROMESTEAD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            return {
                "status": "not_created",
                "message": "Romestead 서버 컨테이너가 아직 생성되지 않았습니다.",
            }

        container.reload()

        if container.status != "running":
            return {
                "status": container.status,
                "message": "Romestead 서버가 실행 중이 아닙니다.",
                "container": container.name,
            }

        container.stop(timeout=15)

        return {
            "status": "stopped",
            "message": "Romestead 서버를 중지했습니다.",
            "container": container.name,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Romestead 서버 중지 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.post("/api/server/restart")
def restart_server(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        try:
            container = client.containers.get(ROMESTEAD_SERVER_CONTAINER)
        except docker.errors.NotFound:
            return {
                "status": "not_created",
                "message": "Romestead 서버 컨테이너가 없습니다. 먼저 서버 시작을 눌러주세요.",
            }

        container.reload()

        if container.status == "running":
            container.restart(timeout=15)

            return {
                "status": "restarted",
                "message": "Romestead 서버를 재시작했습니다.",
                "container": container.name,
            }

        container.start()

        return {
            "status": "started",
            "message": "중지되어 있던 Romestead 서버를 다시 시작했습니다.",
            "container": container.name,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Romestead 서버 재시작 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.get("/api/server/status")
def server_status(request: Request):
    require_auth(request)

    try:
        client = docker.from_env()

        containers = client.containers.list(
            all=True,
            filters={"name": ROMESTEAD_SERVER_CONTAINER},
        )

        for container in containers:
            if container.name == ROMESTEAD_SERVER_CONTAINER:
                container.reload()

                return {
                    "status": container.status,
                    "container": container.name,
                    "image": container.image.tags[0] if container.image.tags else container.image.short_id,
                }

        return {
            "status": "not_created",
            "message": "Romestead 서버 컨테이너가 아직 생성되지 않았습니다.",
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
        container = client.containers.get(ROMESTEAD_SERVER_CONTAINER)

        logs = container.logs(
            stdout=True,
            stderr=True,
            tail=200,
        ).decode("utf-8", errors="replace")

        return {
            "status": "ok",
            "log": logs,
        }

    except Exception as e:
        return {
            "status": "error",
            "log": "",
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
        zip_path = SAVE_EXPORT_DIR / f"romestead-saves-{timestamp}.zip"
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
