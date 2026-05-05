#!/usr/bin/env bash
# Reproduce a CI matrix cell locally in Docker.
#
# Usage:
#   scripts/run_ci_locally.sh <python-version> <django-version>
#   scripts/run_ci_locally.sh 3.10 4.2
#   scripts/run_ci_locally.sh 3.12 6.0
#
# Run all the previously-failing combos:
#   scripts/run_ci_locally.sh all
#
# Notes:
# - Uses python:<ver>-slim images (~50 MB pull, cached after first run)
# - Constrains pytest-xdist to 2 workers to match CI runner cores
# - Mounts the current repo read-only into /app
# - Does not run lint (use `black --check`/`isort --check`/`flake8` locally for that)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

run_one() {
    local PY="$1"
    local DJ="$2"
    echo "================================================================"
    echo "Python $PY  +  Django $DJ"
    echo "================================================================"
    docker run --rm \
        -v "$REPO":/app:ro \
        -w /work \
        --tmpfs /work \
        "python:${PY}-slim" \
        bash -c "
            set -e
            cp -r /app/. /work/
            cd /work
            pip install --quiet --upgrade pip
            pip install --quiet 'django~=${DJ}.0'
            pip install --quiet -e '.[dev]'
            pytest -n 2 --tb=short 2>&1 | tail -30
        "
    echo
}

if [ "${1:-}" = "all" ]; then
    # Matrix cells that have failed in CI on this PR
    run_one 3.10 4.2
    run_one 3.10 5.2
    run_one 3.12 5.2
    run_one 3.12 6.0
    run_one 3.13 6.0
    run_one 3.14 6.0
elif [ $# -eq 2 ]; then
    run_one "$1" "$2"
else
    echo "Usage: $0 <python> <django>   |   $0 all"
    exit 1
fi
