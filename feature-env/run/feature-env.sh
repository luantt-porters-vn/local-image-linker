#!/usr/bin/env bash
# Shared entry point: find the Python implementation above run/, regardless of cwd.
set -euo pipefail
# Replace the shell so exit codes and signals reach the caller unchanged.
exec python3 "$(dirname "$(readlink -f "$0")")/../feature-env.py" "$@"