#!/bin/bash
set -e

LOG_FILE="/var/log/techtim-romestead-install.log"
INSTALL_DIR="/opt/techtim/romestead"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"

GAME_CODE="romestead"
VALID_INSTALL_CODE="RM-2026-GCP-AABB22112211"

{
  echo "======================================"
  echo "TechTim Romestead Startup Script Code Verify Test"
  echo "Started at: $(date)"
  echo "======================================"

  apt-get update -y
  apt-get install -y curl ca-certificates

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

  {
    echo "Romestead Web UI startup script executed successfully."
    echo "game-code=$GAME_CODE"
    echo "install-code=$INSTALL_CODE"
    echo "verify-result=OK"
  } > "$INSTALL_DIR/install-test.txt"

  echo "Created test file: $INSTALL_DIR/install-test.txt"

  echo "======================================"
  echo "Completed at: $(date)"
  echo "======================================"
} | tee -a "$LOG_FILE"
