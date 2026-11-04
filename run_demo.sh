#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTHONPATH="$ROOT:$ROOT/energy/src"
python3 -B -m living_energy --root "${TMPDIR:-/tmp}/openline-living-energy-demo-$RANDOM" demo
