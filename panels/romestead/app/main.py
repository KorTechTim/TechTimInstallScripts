from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
from datetime import datetime
from pathlib import Path
import os
import time
import docker

ROMESTEAD_SERVER_CONTAINER = "romestead-server"
DOTNET_IMAGE = os.getenv("DOTNET_IMAGE", "mcr.microsoft.com/dotnet/runtime:8.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8050"))

app = FastAPI(title="TechTim Romestead Server Panel")

GAME_CODE = os.getenv("GAME_CODE", "romestead")
PANEL_VERSION = os.getenv("PANEL_VERSION", "0.1.4")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.getenv("HOST_DATA_DIR", "/opt/techtim/romestead/data"))
STEAMCMD_IMAGE = os.getenv("STEAMCMD_IMAGE", "steamcmd/steamcmd:ubuntu")
ROMESTEAD_APP_ID = os.getenv("ROMESTEAD_APP_ID", "4763510")

INSTALL_REQUEST_FILE = DATA_DIR / "install-request.txt"
INSTALL_LOG_FILE = DATA_DIR / "install.log"
INSTALL_STATUS_FILE = DATA_DIR / "install-status.txt"


def write_log(message: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    with INSTALL_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")


def set_status(status: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_STATUS_FILE.write_text(status, encoding="utf-8")


def get_status() -> str:
    if not INSTALL_STATUS_FILE.exists():
        return "not_started"
    return INSTALL_STATUS_FILE.read_text(encoding="utf-8").strip()


def create_default_config():
    server_dir = DATA_DIR / "server"
    server_dir.mkdir(parents=True, exist_ok=True)

    config_path = server_dir / "config.json"

    default_config = {
        "AutoStartWorldName": "world",
        "AutoCreateAndLoadWorld": True,
        "AutoCreateWorldSize": 1,
        "Password": "",
        "Port": SERVER_PORT,
        "MaxPlayers": 10,
        "EnableCheats": False,
    }

    import json
    config_path.write_text(
        json.dumps(default_config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return config_path


def install_romestead_job():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    INSTALL_LOG_FILE.write_text("", encoding="utf-8")
    set_status("running")

    write_log("Romestead Dedicated Server 설치 작업을 시작합니다.")
    write_log("SteamCMD anonymous 로그인을 사용합니다.")
    write_log("Steam 계정 정보 입력은 필요하지 않습니다.")
    write_log(f"Romestead Dedicated Server App ID: {ROMESTEAD_APP_ID}")

    try:
        server_dir = DATA_DIR / "server"
        server_dir.mkdir(parents=True, exist_ok=True)

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

        installed_files = list(server_dir.glob("*"))

        if not installed_files:
            write_log("ERROR: 설치 명령은 종료되었지만 /data/server 폴더가 비어 있습니다.")
            set_status("failed")
            return

        INSTALL_REQUEST_FILE.write_text(
            "TechTim Romestead Dedicated Server install completed.\n"
            f"game={GAME_CODE}\n"
            f"panel_version={PANEL_VERSION}\n"
            f"steam_login=anonymous\n"
            f"app_id={ROMESTEAD_APP_ID}\n"
            f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )

        write_log("Romestead Dedicated Server 파일 다운로드가 완료되었습니다.")
        write_log("다음 단계에서 config.json 생성 및 서버 실행 기능을 연결합니다.")
        set_status("completed")

    except Exception as e:
        write_log(f"ERROR: 설치 작업 중 예외 발생: {e}")
        set_status("failed")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>TechTim Romestead Server Panel</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: #f4f6f8;
      color: #1f2937;
    }}
    .wrap {{
      max-width: 1080px;
      margin: 50px auto;
      background: #ffffff;
      border-radius: 16px;
      padding: 40px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.08);
    }}
    .badge {{
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      background: #e0f2fe;
      color: #0369a1;
      font-size: 14px;
      font-weight: bold;
    }}
    h1 {{
      margin-top: 20px;
      font-size: 34px;
    }}
    .status {{
      margin-top: 24px;
      padding: 20px;
      background: #ecfdf5;
      border: 1px solid #bbf7d0;
      border-radius: 12px;
      color: #166534;
      font-weight: bold;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      margin-top: 24px;
    }}
    .card {{
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 18px;
    }}
    .label {{
      font-size: 13px;
      color: #6b7280;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: 20px;
      font-weight: bold;
    }}
    .actions {{
      margin-top: 30px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 10px;
      padding: 14px 20px;
      font-weight: bold;
      cursor: pointer;
      background: #2563eb;
      color: white;
    }}
    button.secondary {{
      background: #e5e7eb;
      color: #1f2937;
    }}
    button:disabled {{
      opacity: 0.6;
      cursor: not-allowed;
    }}
    .result {{
      margin-top: 24px;
      padding: 16px;
      border-radius: 12px;
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      color: #374151;
      min-height: 22px;
      white-space: pre-line;
    }}
    .log {{
      margin-top: 24px;
      background: #111827;
      color: #d1d5db;
      border-radius: 12px;
      padding: 18px;
      min-height: 280px;
      max-height: 460px;
      overflow: auto;
      font-family: Consolas, Monaco, monospace;
      font-size: 13px;
      white-space: pre-wrap;
    }}
    .note {{
      margin-top: 28px;
      color: #6b7280;
      line-height: 1.6;
    }}
    code {{
      background: #f3f4f6;
      padding: 2px 6px;
      border-radius: 6px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <span class="badge">Web GUI :8080</span>
    <h1>TechTim Romestead Server Panel</h1>
    <p>Romestead GCP 서버 관리 패널 실제 엔진 설치 테스트 버전입니다.</p>

    <div class="status">
      SteamCMD anonymous 로그인 기반 Romestead Dedicated Server 다운로드 가능
    </div>

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
        <div class="label">패널 버전</div>
        <div class="value">{PANEL_VERSION}</div>
      </div>
    </div>

    <div class="actions">
      <button id="installBtn" onclick="requestInstall()">엔진 설치</button>
      <button class="secondary">서버 시작</button>
      <button class="secondary">로그 보기</button>
      <button class="secondary">세이브 관리</button>
      <button class="secondary" onclick="loadLog()">설치 로그 새로고침</button>
    </div>

    <div id="result" class="result">
      아직 실행된 작업이 없습니다.
    </div>

    <div id="installLog" class="log">설치 로그가 여기에 표시됩니다.</div>

    <div class="note">
      이번 단계는 실제 Romestead 서버 파일 다운로드 테스트입니다.<br>
      SteamCMD anonymous 로그인을 사용하므로 Steam 계정 정보 입력은 필요하지 않습니다.<br>
      설치 완료 후 서버 파일은 <code>/data/server</code> 경로에 저장됩니다.
    </div>
  </div>

  <script>
    async function requestInstall() {{
      const btn = document.getElementById("installBtn");
      const result = document.getElementById("result");

      btn.disabled = true;
      result.innerText = "Romestead 엔진 설치 작업을 시작하는 중입니다...";

      try {{
        const response = await fetch("/api/install", {{
          method: "POST"
        }});

        const data = await response.json();

        if (!response.ok) {{
          result.innerText = "오류: " + (data.detail || "설치 요청 실패");
          return;
        }}

        result.innerText =
          "설치 작업 시작됨\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + data.message;

        await loadStatus();
        await loadLog();
      }} catch (err) {{
        result.innerText = "요청 실패: " + err;
      }} finally {{
        btn.disabled = false;
      }}
    }}

    async function loadStatus() {{
      try {{
        const response = await fetch("/api/install/status");
        const data = await response.json();
        document.getElementById("installStatus").innerText = data.status;
      }} catch (err) {{
        document.getElementById("installStatus").innerText = "error";
      }}
    }}

    async function loadLog() {{
      try {{
        const response = await fetch("/api/install/log");
        const data = await response.json();
        document.getElementById("installLog").innerText = data.log || "로그가 없습니다.";
      }} catch (err) {{
        document.getElementById("installLog").innerText = "로그 조회 실패: " + err;
      }}
    }}

    setInterval(loadStatus, 2000);
    setInterval(loadLog, 2000);

    loadStatus();
    loadLog();
  </script>
</body>
</html>
"""


@app.post("/api/install")
def request_install(background_tasks: BackgroundTasks):
    current_status = get_status()

    if current_status == "running":
        return {
            "status": "running",
            "message": "이미 설치 작업이 실행 중입니다.",
        }

    background_tasks.add_task(install_romestead_job)

    return {
        "status": "started",
        "message": "Romestead Dedicated Server 설치 작업이 백그라운드에서 시작되었습니다.",
    }


@app.get("/api/install/status")
def install_status():
    return {
        "status": get_status(),
    }


@app.get("/api/install/log")
def install_log():
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
def docker_status():
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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "game": GAME_CODE,
        "version": PANEL_VERSION,
    }

@app.post("/api/server/start")
def start_server():
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

        client = docker.from_env()

        existing = client.containers.list(
            all=True,
            filters={"name": ROMESTEAD_SERVER_CONTAINER},
        )

        for container in existing:
            if container.name == ROMESTEAD_SERVER_CONTAINER:
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
                f"{SERVER_PORT}/udp": SERVER_PORT,
            },
        )

        return {
            "status": "started",
            "message": "Romestead 서버 컨테이너를 시작했습니다.",
            "container": container.name,
            "config": str(config_path),
            "port": f"{SERVER_PORT}/udp",
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "Romestead 서버 시작 중 오류가 발생했습니다.",
            "error": str(e),
        }


@app.get("/api/server/status")
def server_status():
    try:
        client = docker.from_env()

        containers = client.containers.list(
            all=True,
            filters={"name": ROMESTEAD_SERVER_CONTAINER},
        )

        for container in containers:
            if container.name == ROMESTEAD_SERVER_CONTAINER:
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
