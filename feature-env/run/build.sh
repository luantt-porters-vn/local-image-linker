#!/usr/bin/env bash
# Build local source images without deploying; forward selectors and build options.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" build "$@"