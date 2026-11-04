#!/usr/bin/env sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"
exec python3 -m autopilot "$@"
