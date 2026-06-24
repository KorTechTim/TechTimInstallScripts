#!/bin/bash
set -e

LOG_FILE="/var/log/techtim-romestead-install.log"
INSTALL_DIR="/opt/techtim/romestead"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"

GAME_CODE="romestead"
VALID_INSTALL_CODE="RM-2026-GCP-AABB22112211"

{
  echo "======================================"
  echo "TechTim Romestead Web UI Install Test with Basic Auth"
  echo "Started at: $(date)"
  echo "======================================"

  apt-get update -y
  apt-get install -y curl ca-certificates gnupg openssl apache2-utils

  mkdir -p "$INSTALL_DIR"

  echo "Reading install-code from GCP metadata..."

  INSTALL_CODE=$(curl -s -H "Metadata-Flavor: Google" "$METADATA_URL" || true)

  if [ -z "$INSTALL_CODE" ]; then
    echo "ERROR: install-code metadata is missing."
    echo "ERROR: install-code metadata is missing." > "$INSTALL_DIR/install-test.txt"
    exit 1
  fi

  echo "install-code metadata found."
  echo "Game code: $GAME_CODE"
  echo "Install code: $INSTALL_CODE"

  if [ "$INSTALL_CODE" != "$VALID_INSTALL_CODE" ]; then
    echo "ERROR: Invalid install code."
    echo "ERROR: Invalid install code." > "$INSTALL_DIR/install-test.txt"
    exit 1
  fi

  echo "Install code verified."

  echo "Installing Docker..."

  install -m 0755 -d /etc/apt/keyrings

  if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
  fi

  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  echo "Docker installed."

  cd "$INSTALL_DIR"

  ADMIN_PASSWORD=$(openssl rand -base64 12)
  echo "$ADMIN_PASSWORD" > "$INSTALL_DIR/admin_password.txt"
  chmod 600 "$INSTALL_DIR/admin_password.txt"

  mkdir -p html nginx

  htpasswd -bc "$INSTALL_DIR/nginx/.htpasswd" admin "$ADMIN_PASSWORD"

  cat > "$INSTALL_DIR/nginx/default.conf" <<'EOF'
server {
    listen 80;
    server_name _;

    auth_basic "TechTim Romestead Server Panel";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ =404;
    }
}
EOF

  cat > html/index.html <<'EOF'
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>TechTim Romestead Server Panel</title>
  <style>
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: #f4f6f8;
      color: #1f2937;
    }
    .wrap {
      max-width: 900px;
      margin: 80px auto;
      background: #ffffff;
      border-radius: 16px;
      padding: 40px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.08);
    }
    .badge {
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      background: #e0f2fe;
      color: #0369a1;
      font-size: 14px;
      font-weight: bold;
    }
    h1 {
      margin-top: 20px;
      font-size: 34px;
    }
    .status {
      margin-top: 24px;
      padding: 20px;
      background: #ecfdf5;
      border: 1px solid #bbf7d0;
      border-radius: 12px;
      color: #166534;
      font-weight: bold;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      margin-top: 24px;
    }
    .card {
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 18px;
    }
    .label {
      font-size: 13px;
      color: #6b7280;
      margin-bottom: 8px;
    }
    .value {
      font-size: 20px;
      font-weight: bold;
    }
    .note {
      margin-top: 28px;
      color: #6b7280;
      line-height: 1.6;
    }
    code {
      background: #f3f4f6;
      padding: 2px 6px;
      border-radius: 6px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <span class="badge">Web GUI :8080</span>
    <h1>TechTim Romestead Server Panel</h1>
    <p>Romestead GCP 서버 관리 패널 테스트 화면입니다.</p>

    <div class="status">
      관리자 로그인 보호 적용 완료 · Web UI 자동 설치 성공
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
        <div class="label">관리 포트</div>
        <div class="value">8080 TCP</div>
      </div>
    </div>

    <div class="note">
      이 화면이 보이면 GitHub startup script, 설치 코드 검증, Docker 설치,
      Web UI 컨테이너 실행, 관리자 로그인 보호까지 정상적으로 완료된 것입니다.<br>
      관리자 비밀번호 파일 위치: <code>/opt/techtim/romestead/admin_password.txt</code>
    </div>
  </div>
</body>
</html>
EOF

  cat > docker-compose.yml <<'EOF'
services:
  romestead-webui:
    image: nginx:alpine
    container_name: romestead-webui
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/.htpasswd:/etc/nginx/.htpasswd:ro
EOF

  docker compose up -d

  {
    echo "Romestead Web UI startup script executed successfully."
    echo "game-code=$GAME_CODE"
    echo "install-code=$INSTALL_CODE"
    echo "verify-result=OK"
    echo "docker=installed"
    echo "webui=running"
    echo "basic-auth=enabled"
    echo "admin-username=admin"
    echo "admin-password-file=$INSTALL_DIR/admin_password.txt"
  } > "$INSTALL_DIR/install-test.txt"

  echo "======================================"
  echo "TechTim Romestead Web UI Installed"
  echo "URL: http://VM_EXTERNAL_IP:8080"
  echo "Admin Username: admin"
  echo "Admin Password: $ADMIN_PASSWORD"
  echo "Password file: $INSTALL_DIR/admin_password.txt"
  echo "======================================"

  echo "Completed at: $(date)"
} | tee -a "$LOG_FILE"
