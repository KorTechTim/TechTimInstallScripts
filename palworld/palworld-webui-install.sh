#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/techtim-palworld-install.log"
INSTALL_DIR="/opt/techtim/palworld"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"

GAME_CODE="palworld"
VERIFY_API="https://techtim.kr/api/install/verify"
PANEL_IMAGE="ghcr.io/kortechtim/palworld-panel:latest"
PANEL_VERSION="1.0.0"

exec > >(tee -a "$LOG_FILE") 2>&1

write_status() {
  mkdir -p "$INSTALL_DIR"
  {
    for line in "$@"; do
      echo "$line"
    done
  } > "$INSTALL_DIR/install-status.txt"
}

mask_code() {
  local code="$1"
  local len=${#code}

  if [ "$len" -le 8 ]; then
    echo "********"
    return
  fi

  echo "${code:0:4}...${code: -4}"
}

configure_timezone() {
  local timezone="Asia/Seoul"

  echo "Configuring OS timezone: $timezone"

  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$timezone" || true
  fi

  if [ -f "/usr/share/zoneinfo/$timezone" ]; then
    ln -snf "/usr/share/zoneinfo/$timezone" /etc/localtime
    echo "$timezone" > /etc/timezone
  fi

  echo "Current timezone: $(cat /etc/timezone 2>/dev/null || echo "$timezone")"
  echo "Current time: $(date)"
}

allow_iptables_port() {
  local chain="$1"
  local protocol="$2"
  local port="$3"

  if ! iptables -nL "$chain" >/dev/null 2>&1; then
    return
  fi

  if iptables -C "$chain" -p "$protocol" --dport "$port" -j ACCEPT >/dev/null 2>&1; then
    echo "iptables rule already exists: $chain $protocol/$port"
    return
  fi

  iptables -I "$chain" 1 -p "$protocol" --dport "$port" -j ACCEPT
  echo "iptables rule added: $chain $protocol/$port"
}

configure_host_firewall() {
  echo "Configuring host iptables rules for Palworld..."

  if ! command -v iptables >/dev/null 2>&1; then
    echo "WARNING: iptables command not found. Skipping host firewall rules."
    return
  fi

  allow_iptables_port INPUT tcp 8080
  allow_iptables_port INPUT udp 8211
  allow_iptables_port INPUT tcp 8212
  allow_iptables_port INPUT tcp 25575

  allow_iptables_port DOCKER-USER tcp 8080
  allow_iptables_port DOCKER-USER udp 8211
  allow_iptables_port DOCKER-USER tcp 8212
  allow_iptables_port DOCKER-USER tcp 25575
}

configure_timezone

echo "======================================"
echo "TechTim Palworld Panel Install"
echo "Started at: $(date)"
echo "======================================"

apt-get update -y
apt-get install -y curl ca-certificates gnupg

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups" "$INSTALL_DIR/uploads" "$INSTALL_DIR/nginx"

echo "Reading install-code from GCP metadata..."
INSTALL_CODE=$(curl -fsS -H "Metadata-Flavor: Google" "$METADATA_URL" || true)

if [ -z "$INSTALL_CODE" ]; then
  echo "ERROR: install-code metadata is missing."
  write_status \
    "verify-result=missing-install-code" \
    "webui=not-started"
  exit 1
fi

echo "install-code metadata found: $(mask_code "$INSTALL_CODE")"
echo "Game code: $GAME_CODE"
echo "Verifying install-code with TechTim API..."

VERIFY_RESULT=$(curl -fsSL "${VERIFY_API}?game=${GAME_CODE}&code=${INSTALL_CODE}" || true)

if [ "$VERIFY_RESULT" != "OK" ]; then
  echo "ERROR: Invalid install code."
  echo "VERIFY_RESULT=$VERIFY_RESULT"
  write_status \
    "verify-result=${VERIFY_RESULT:-DENY}" \
    "webui=not-started"
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
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
configure_host_firewall

echo "Docker installed."
cd "$INSTALL_DIR"

cat > "$INSTALL_DIR/nginx/default.conf" <<'NGINX_CONF'
server {
    listen 80;
    server_name _;

    client_max_body_size 4096M;
    client_body_timeout 3600s;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://palworld-panel:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX_CONF

cat > docker-compose.yml <<COMPOSE
services:
  palworld-panel:
    image: ${PANEL_IMAGE}
    container_name: palworld-panel
    restart: unless-stopped
    environment:
      - GAME_CODE=${GAME_CODE}
      - INSTALL_CODE=${INSTALL_CODE}
      - PANEL_VERSION=${PANEL_VERSION}
      - DATA_DIR=/data
      - HOST_DATA_DIR=${INSTALL_DIR}/data
      - PALWORLD_RUNTIME_IMAGE=ghcr.io/pocketpairjp/palserver:latest
      - PALWORLD_UPDATE_IMAGE=ghcr.io/pocketpairjp/palserver:latest
      - SERVER_PORT=8211
      - RCON_PORT=25575
      - PALWORLD_SERVER_CONTAINER=palworld-server
    volumes:
      - ${INSTALL_DIR}/data:/data
      - ${INSTALL_DIR}/backups:/backups
      - ${INSTALL_DIR}/uploads:/uploads
      - /var/run/docker.sock:/var/run/docker.sock

  palworld-panel-proxy:
    image: nginx:alpine
    container_name: palworld-panel-proxy
    restart: unless-stopped
    ports:
      - "8080:80"
    depends_on:
      - palworld-panel
    volumes:
      - ${INSTALL_DIR}/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
COMPOSE

echo "Pulling and starting Palworld Panel image..."
docker compose pull
docker compose up -d --remove-orphans

write_status \
  "verify-result=OK" \
  "panel-image=$PANEL_IMAGE" \
  "docker=installed" \
  "webui=running" \
  "auth-mode=fastapi-login" \
  "default-username=admin" \
  "default-password=admin" \
  "first-login-password-change=required"

echo "======================================"
echo "TechTim Palworld Panel Installed"
echo "URL: http://VM_EXTERNAL_IP:8080"
echo "Default Username: admin"
echo "Default Password: admin"
echo "First login requires password change."
echo "Status file: $INSTALL_DIR/install-status.txt"
echo "======================================"
echo "Completed at: $(date)"
