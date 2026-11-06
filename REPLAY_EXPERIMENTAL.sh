#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTHONPATH="$ROOT:$ROOT/energy/src:$ROOT/src"
(cd "$ROOT" && sha256sum -c SHA256SUMS.txt > /dev/null)
python3 -B -m unittest discover -s "$ROOT/energy/tests" -v
python3 -B "$ROOT/scripts/run_portable_tests.py"
