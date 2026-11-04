#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTHONPATH="$ROOT:$ROOT/energy/src:$ROOT/src"
(cd "$ROOT" && sha256sum -c SHA256SUMS.txt > /dev/null)
python3 -B -m unittest discover -s "$ROOT/energy/tests" -v
if unshare --user --map-root-user --mount --net --pid --fork true >/dev/null 2>&1 && command -v ip >/dev/null; then
  python3 -B -m unittest discover -s "$ROOT/tests" -v
else
  echo 'SANDBOX_NOT_TESTED_ON_THIS_HOST: running portable non-sandbox suite only'
  python3 -B "$ROOT/scripts/run_portable_tests.py"
fi
