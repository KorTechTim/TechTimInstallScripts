#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/techtim-minecraft-install.log"
INSTALL_DIR="/opt/techtim/minecraft"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"
GAME_CODE="minecraft"
VERIFY_API="https://techtim.kr/api/install/verify"
PANEL_IMAGE="ghcr.io/kortechtim/minecraft-panel:latest"
PANEL_VERSION="1.0.0"

exec > >(tee -a "$LOG_FILE") 2>&1

write_status() {
  mkdir -p "$INSTALL_DIR"
  printf '%s\n' "$@" > "$INSTALL_DIR/install-status.txt"
}

mask_code() {
  local code="$1"
  if [ "${#code}" -le 8 ]; then echo "********"; else echo "${code:0:4}...${code: -4}"; fi
}

configure_timezone() {
  local timezone="Asia/Seoul"
  timedatectl set-timezone "$timezone" 2>/dev/null || true
  ln -snf "/usr/share/zoneinfo/$timezone" /etc/localtime
  echo "$timezone" > /etc/timezone
  echo "Timezone configured: $timezone ($(date))"
}

allow_iptables_port() {
  local chain="$1" protocol="$2" port="$3"
  iptables -nL "$chain" >/dev/null 2>&1 || return 0
  iptables -C "$chain" -p "$protocol" --dport "$port" -j ACCEPT >/dev/null 2>&1 || \
    iptables -I "$chain" 1 -p "$protocol" --dport "$port" -j ACCEPT
}

configure_firewall() {
  command -v iptables >/dev/null 2>&1 || return 0
  for chain in INPUT DOCKER-USER; do
    allow_iptables_port "$chain" tcp 8080
    allow_iptables_port "$chain" tcp 25565
    allow_iptables_port "$chain" udp 25565
  done
}

configure_timezone
echo "======================================"
echo "TechTim Minecraft Panel Install"
echo "Started at: $(date)"
echo "======================================"

apt-get update -y
apt-get install -y curl ca-certificates gnupg
mkdir -p "$INSTALL_DIR/data/server" "$INSTALL_DIR/backups" "$INSTALL_DIR/uploads" "$INSTALL_DIR/nginx"

echo "Reading install-code from GCP metadata..."
INSTALL_CODE=$(curl -fsS -H "Metadata-Flavor: Google" "$METADATA_URL" || true)
if [ -z "$INSTALL_CODE" ]; then
  write_status "verify-result=missing-install-code" "webui=not-started"
  echo "ERROR: install-code metadata is missing."
  exit 1
fi

echo "Install code: $(mask_code "$INSTALL_CODE")"
VERIFY_RESULT=$(curl -fsSL "${VERIFY_API}?game=${GAME_CODE}&code=${INSTALL_CODE}" || true)
if [ "$VERIFY_RESULT" != "OK" ]; then
  write_status "verify-result=${VERIFY_RESULT:-DENY}" "webui=not-started"
  echo "ERROR: Invalid install code. Result=$VERIFY_RESULT"
  exit 1
fi

install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
configure_firewall

cat > "$INSTALL_DIR/nginx/default.conf" <<'NGINX_CONF'
server {
    listen 80;
    server_name _;
    client_max_body_size 4096M;
    proxy_read_timeout 3600s;

    location / {
        proxy_pass http://minecraft-panel:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX_CONF

cat > "$INSTALL_DIR/docker-compose.yml" <<COMPOSE
services:
  minecraft-panel:
    image: ${PANEL_IMAGE}
    container_name: minecraft-panel
    restart: unless-stopped
    environment:
      - GAME_CODE=${GAME_CODE}
      - INSTALL_CODE=${INSTALL_CODE}
      - PANEL_VERSION=${PANEL_VERSION}
      - DATA_DIR=/data
      - HOST_DATA_DIR=${INSTALL_DIR}/data
      - MINECRAFT_RUNTIME_IMAGE=itzg/minecraft-server:latest
      - MINECRAFT_SERVER_CONTAINER=minecraft-server
      - SERVER_PORT=25565
    volumes:
      - ${INSTALL_DIR}/data:/data
      - ${INSTALL_DIR}/backups:/backups
      - ${INSTALL_DIR}/uploads:/uploads
      - /var/run/docker.sock:/var/run/docker.sock

  minecraft-panel-proxy:
    image: nginx:alpine
    container_name: minecraft-panel-proxy
    restart: unless-stopped
    ports:
      - "8080:80"
    depends_on:
      - minecraft-panel
    volumes:
      - ${INSTALL_DIR}/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
COMPOSE

cd "$INSTALL_DIR"
docker compose pull
docker compose up -d --remove-orphans

write_status \
  "verify-result=OK" \
  "panel-image=$PANEL_IMAGE" \
  "docker=installed" \
  "webui=running" \
  "default-username=admin" \
  "default-password=admin"

echo "======================================"
echo "TechTim Minecraft Panel Installed"
echo "URL: http://VM_EXTERNAL_IP:8080"
echo "Minecraft: VM_EXTERNAL_IP:25565"
echo "Default login: admin / admin"
echo "Completed at: $(date)"
echo "======================================"
