#!/bin/bash
set -e

LOG_FILE="/var/log/techtim-romestead-install.log"
INSTALL_DIR="/opt/techtim/romestead"
METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/install-code"

{
  echo "======================================"
  echo "TechTim Romestead Startup Script Metadata Test"
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
  echo "Install code: $INSTALL_CODE"

  {
    echo "Romestead Web UI startup script executed successfully."
    echo "install-code=$INSTALL_CODE"
  } > "$INSTALL_DIR/install-test.txt"

  echo "Created test file: $INSTALL_DIR/install-test.txt"

  echo "======================================"
  echo "Completed at: $(date)"
  echo "======================================"
} | tee -a "$LOG_FILE"
