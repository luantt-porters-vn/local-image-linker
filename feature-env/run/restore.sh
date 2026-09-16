#!/usr/bin/env bash
# Restore selected applications to saved normal images, not source or database data.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" restore "$@"