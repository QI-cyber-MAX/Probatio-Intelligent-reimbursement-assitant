#!/usr/bin/env bash
set -euo pipefail

# Only these directories are prepared; existing business files retain their modes.
install -d -o 10001 -g 10001 -m 0700 \
  /data/managed /data/managed/quarantine /data/managed/originals \
  /data/managed/parsed /data/managed/chunks /data/managed/wiki /data/managed/diagnostics \
  /data/managed/queue_anomalies /work/parser
install -d -o 0 -g 10001 -m 2770 /run/clamav
