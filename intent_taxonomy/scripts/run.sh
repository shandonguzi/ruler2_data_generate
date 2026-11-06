#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

python main.py discover \
  --input "${1:-intent_data.xlsx}" \
  --iteration-rounds "${2:-3}"
