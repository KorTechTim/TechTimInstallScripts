#!/bin/bash
set -e

LOG_FILE="/var/log/techtim-romestead-install.log"
INSTALL_DIR="/opt/techtim/romestead"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"

GAME_CODE="romestead"
VERIFY_API="https://techtim.kr/api/install/verify"
PANEL_IMAGE="ghcr.io/kortechtim/romestead-panel:latest"

{
  echo "======================================"
  echo "TechTim Romestead Panel Install"
  echo "Started at: $(date)"
  echo "======================================"

  apt-get update -y
  apt-get install -y curl ca-certificates gnupg openssl apache2-utils

  mkdir -p "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR/data"
  mkdir -p "$INSTALL_DIR/backups"
  mkdir -p "$INSTALL_DIR/uploads"
  mkdir -p "$INSTALL_DIR/nginx"

  echo "Reading install-code from GCP metadata..."

  INSTALL_CODE=$(curl -s -H "Metadata-Flavor: Google" "$METADATA_URL" || true)

  if [ -z "$INSTALL_CODE" ]; then
    echo "ERROR: install-code metadata is missing."
    echo "ERROR: install-code metadata is missing." > "$INSTALL_DIR/install-status.txt"
    exit 1
  fi

  echo "install-code metadata found."
  echo "Game code: $GAME_CODE"
  echo "Install code: $INSTALL_CODE"

  echo "Verifying install-code with TechTim API..."

  VERIFY_RESULT=$(curl -fsSL "${VERIFY_API}?game=${GAME_CODE}&code=${INSTALL_CODE}" || true)

  if [ "$VERIFY_RESULT" != "OK" ]; then
    echo "ERROR: Invalid install code."
    echo "VERIFY_RESULT=$VERIFY_RESULT"
    echo "ERROR: Invalid install code." > "$INSTALL_DIR/install-status.txt"
    exit 1
  fi

  echo "Install code verified by TechTim API."

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

  htpasswd -bc "$INSTALL_DIR/nginx/.htpasswd" admin "$ADMIN_PASSWORD"

  cat > "$INSTALL_DIR/nginx/default.conf" <<'EOF'
server {
    listen 80;
    server_name _;

    auth_basic "TechTim Romestead Server Panel";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://romestead-panel:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

  cat > docker-compose.yml <<EOF
services:
  romestead-panel:
    image: ${PANEL_IMAGE}
    container_name: romestead-panel
    restart: unless-stopped
  environment:
    - GAME_CODE=${GAME_CODE}
    - INSTALL_CODE=${INSTALL_CODE}
    - PANEL_VERSION=0.1.3
    - DATA_DIR=/data
    - HOST_DATA_DIR=${INSTALL_DIR}/data
    - STEAMCMD_IMAGE=steamcmd/steamcmd:ubuntu
  volumes:
      - ${INSTALL_DIR}/data:/data
      - ${INSTALL_DIR}/backups:/backups
      - ${INSTALL_DIR}/uploads:/uploads
      - /var/run/docker.sock:/var/run/docker.sock

  romestead-panel-proxy:
    image: nginx:alpine
    container_name: romestead-panel-proxy
    restart: unless-stopped
    ports:
      - "8080:80"
    depends_on:
      - romestead-panel
    volumes:
      - ${INSTALL_DIR}/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ${INSTALL_DIR}/nginx/.htpasswd:/etc/nginx/.htpasswd:ro
EOF

  echo "Pulling and starting Romestead Panel image..."
  docker compose pull
  docker compose up -d

  {
    echo "Romestead Web UI startup script executed successfully."
    echo "game-code=$GAME_CODE"
    echo "install-code=$INSTALL_CODE"
    echo "verify-api=$VERIFY_API"
    echo "verify-result=OK"
    echo "panel-image=$PANEL_IMAGE"
    echo "docker=installed"
    echo "webui=running"
    echo "basic-auth=enabled"
    echo "admin-username=admin"
    echo "admin-password-file=$INSTALL_DIR/admin_password.txt"
  } > "$INSTALL_DIR/install-status.txt"

  echo "======================================"
  echo "TechTim Romestead Panel Installed"
  echo "URL: http://VM_EXTERNAL_IP:8080"
  echo "Admin Username: admin"
  echo "Admin Password: $ADMIN_PASSWORD"
  echo "Password file: $INSTALL_DIR/admin_password.txt"
  echo "Status file: $INSTALL_DIR/install-status.txt"
  echo "======================================"

  echo "Completed at: $(date)"
} | tee -a "$LOG_FILE"
