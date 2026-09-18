#!/usr/bin/env bash
# Remove local feature images and stale build snapshots not referenced by saved state.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" clean "$@"
