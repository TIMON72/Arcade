#!/bin/bash
# Развёртывание Arcade Timer в /userdata/system/ (Batocera).
# Использование:
#   ./deploy.sh              # принудительный deploy
#   ./deploy.sh --restart    # deploy + перезапуск сервиса main

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_PY="$PROJECT_ROOT/scripts/main.py"
DEPLOYED_SCRIPTS="/userdata/system/scripts"
RESTART=0

usage() {
    echo "Usage: $0 [--restart]"
    echo "  --restart   перезапустить batocera-services main после deploy"
}

for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Unknown option: $arg" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ ! -f "$MAIN_PY" ]; then
    echo "ERROR: main.py not found: $MAIN_PY" >&2
    exit 1
fi

for dir in configs services scripts; do
    if [ ! -d "$PROJECT_ROOT/$dir" ]; then
        echo "ERROR: missing $PROJECT_ROOT/$dir (run from project root)" >&2
        exit 1
    fi
done

if [ ! -f "$PROJECT_ROOT/batocera.conf" ]; then
    echo "ERROR: missing $PROJECT_ROOT/batocera.conf" >&2
    exit 1
fi

if [ "$(realpath "$PROJECT_ROOT/scripts")" = "$(realpath "$DEPLOYED_SCRIPTS" 2>/dev/null || echo "")" ]; then
    echo "ERROR: deploy must run from the project copy, not $DEPLOYED_SCRIPTS" >&2
    echo "  Example: cd /userdata/system/Arcade && ./deploy.sh" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found" >&2
    exit 1
fi

echo "Project: $PROJECT_ROOT"
python3 "$MAIN_PY" deploy

if [ "$RESTART" -eq 1 ]; then
    if command -v batocera-services >/dev/null 2>&1; then
        echo "Restarting service main..."
        batocera-services restart main
        batocera-services status main || true
    elif [ -x /userdata/system/services/main ]; then
        echo "Restarting /userdata/system/services/main..."
        /userdata/system/services/main restart
        /userdata/system/services/main status || true
    else
        echo "WARN: batocera-services not found; start manually:" >&2
        echo "  /userdata/system/services/main restart" >&2
    fi
fi
