from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os

app = FastAPI(title="TechTim Romestead Server Panel")

GAME_CODE = os.getenv("GAME_CODE", "romestead")
PANEL_VERSION = os.getenv("PANEL_VERSION", "0.1.0")


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
    <p>Romestead GCP 서버 관리 패널 초기 버전입니다.</p>

    <div class="status">
      실제 Docker 이미지 기반 Web UI 실행 준비 완료
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
      <button>엔진 설치</button>
      <button class="secondary">서버 시작</button>
      <button class="secondary">로그 보기</button>
      <button class="secondary">세이브 관리</button>
    </div>

    <div class="note">
      이 화면은 <code>panels/romestead</code> 소스에서 빌드될 Web UI 테스트 버전입니다.<br>
      다음 단계에서 GitHub Actions로 Docker 이미지를 만들고,
      startup script에서 해당 이미지를 실행하도록 변경합니다.
    </div>
  </div>
</body>
</html>
"""


@app.get("/health")
def health():
    return {
        "status": "ok",
        "game": GAME_CODE,
        "version": PANEL_VERSION
    }
