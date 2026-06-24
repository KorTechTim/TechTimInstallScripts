from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime
from pathlib import Path
import os

app = FastAPI(title="TechTim Romestead Server Panel")

GAME_CODE = os.getenv("GAME_CODE", "romestead")
PANEL_VERSION = os.getenv("PANEL_VERSION", "0.1.1")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INSTALL_REQUEST_FILE = DATA_DIR / "install-request.txt"


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
      max-width: 980px;
      margin: 60px auto;
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
    <p>Romestead GCP 서버 관리 패널 초기 기능 테스트 버전입니다.</p>

    <div class="status">
      FastAPI 기반 Web UI 실행 중 · 엔진 설치 요청 API 테스트 가능
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">게임</div>
        <div class="value">Romestead</div>
      </div>
      <div class="card">
        <div class="label">서버 상태</div>
        <div class="value">대기 중</div>
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
    </div>

    <div id="result" class="result">
      아직 실행된 작업이 없습니다.
    </div>

    <div class="note">
      이번 단계는 실제 SteamCMD 설치 전 테스트입니다.<br>
      <code>엔진 설치</code> 버튼을 누르면 <code>/api/install</code>이 호출되고,
      VM 내부 <code>/data/install-request.txt</code> 파일이 생성됩니다.
    </div>
  </div>

  <script>
    async function requestInstall() {{
      const btn = document.getElementById("installBtn");
      const result = document.getElementById("result");

      btn.disabled = true;
      result.innerText = "엔진 설치 요청을 전송하는 중입니다...";

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
          "설치 요청 완료\\n" +
          "상태: " + data.status + "\\n" +
          "메시지: " + data.message + "\\n" +
          "기록 파일: " + data.request_file;
      }} catch (err) {{
        result.innerText = "요청 실패: " + err;
      }} finally {{
        btn.disabled = false;
      }}
    }}
  </script>
</body>
</html>
"""


@app.post("/api/install")
def request_install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat(timespec="seconds")

    content = (
        "TechTim Romestead install request received.\n"
        f"game={GAME_CODE}\n"
        f"panel_version={PANEL_VERSION}\n"
        f"requested_at={now}\n"
    )

    INSTALL_REQUEST_FILE.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "message": "Romestead 엔진 설치 요청이 기록되었습니다.",
        "request_file": str(INSTALL_REQUEST_FILE),
        "requested_at": now,
    }


@app.get("/api/install/status")
def install_status():
    if not INSTALL_REQUEST_FILE.exists():
        return {
            "status": "not_requested",
            "message": "아직 설치 요청이 없습니다.",
        }

    return {
        "status": "requested",
        "message": "설치 요청 파일이 존재합니다.",
        "request_file": str(INSTALL_REQUEST_FILE),
        "content": INSTALL_REQUEST_FILE.read_text(encoding="utf-8"),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "game": GAME_CODE,
        "version": PANEL_VERSION,
    }
