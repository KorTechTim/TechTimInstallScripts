#!/bin/bash
set -e

LOG_FILE="/var/log/techtim-romestead-install.log"
INSTALL_DIR="/opt/techtim/romestead"

{
  echo "======================================"
  echo "TechTim Romestead Startup Script Test"
  echo "Started at: $(date)"
  echo "======================================"

  apt-get update -y
  apt-get install -y curl ca-certificates

  mkdir -p "$INSTALL_DIR"

  echo "Romestead Web UI startup script executed successfully." > "$INSTALL_DIR/install-test.txt"
  echo "Created test file: $INSTALL_DIR/install-test.txt"

  echo "======================================"
  echo "Completed at: $(date)"
  echo "======================================"
} | tee -a "$LOG_FILE"
