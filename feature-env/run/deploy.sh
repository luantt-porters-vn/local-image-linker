#!/usr/bin/env bash
# Deploy previously built images; --only and --exclude select application services.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" up "$@"