# Run Local Images

Run from this folder. Requires Linux/WSL, Bash, Python 3.9+, Git, Docker BuildKit,
Compose with `up --wait` and `config --format json`, an existing HRBC cluster,
private registry access, and a container mounting `devcontainer-settings`.

Shell launchers are in `run/`; Dockerfiles are in `docker/`. Run the commands below
from this folder, leaving `.env` and existing state in their current locations.

When moving this folder after a deployment, set `FEATURE_ENV_STATE_DIR` to the
absolute path of your existing state directory. Relative paths resolve beside
`.env` and change meaning after a move. Keep the saved state; it contains the
original images needed for restore. If build fails, fix it before running deploy.

`run/feature-env.sh` is the shared launcher for the Python implementation.
The build, deploy and restore scripts call it with their command; use it directly
for `paths`, `status` and `stop`. It does not start another service.

## Configure

After cloning from GitHub, manually create `.env` in this folder with your local
repository roots. If it already exists, edit it while keeping your existing state path:

```dotenv
FEATURE_ENV_UI_REPO="$HOME/work/ui"
FEATURE_ENV_PROXY_REPO="$HOME/work/openapi-proxy"
FEATURE_ENV_HRBC_REPO="$HOME/work/hrbc"
FEATURE_ENV_CLIENT_REPO="$HOME/work/api-client-privateapi"
FEATURE_ENV_STATE_DIR=".feature-env"
```

```bash
bash run/feature-env.sh paths
```

## Build And Deploy

Save changes and disconnect Dev Container aliases for selected services first.
The first build automatically captures restore state. Build does not deploy.

```bash
bash run/build.sh
bash run/deploy.sh
```

Build progress updates in place in an interactive terminal, with one row per
image showing its status, elapsed time, and current Docker step. When output is
redirected (or `TERM=dumb`), it prints status changes and a progress update every
15 seconds instead. Full Docker output is saved in the displayed
`builds/<build-id>/<target>.log` files; failures include the relevant log path.

## Restore

Return to saved normal application images, without rolling back the database:

```bash
bash run/restore.sh
```

## Only Selected Services

```bash
bash run/build.sh --only web
bash run/deploy.sh --only web

bash run/build.sh --only ui openapi-proxy
bash run/deploy.sh --only ui openapi-proxy

# Restore selected services when finished:
bash run/restore.sh --only web
bash run/restore.sh --only ui openapi-proxy
```

## Exclude Services

```bash
bash run/build.sh --exclude ui
bash run/deploy.sh --exclude ui

bash run/build.sh --exclude ui openapi-proxy
bash run/deploy.sh --exclude ui openapi-proxy

# Restore without touching excluded services:
bash run/restore.sh --exclude ui
bash run/restore.sh --exclude ui openapi-proxy
```

Targets: `ui`, `openapi-proxy` (or `proxy`), `api`, `web`, `hrbc` (api + web),
`api-client-privateapi` (ui). UI builds include the local client library.
Use either `--only` or `--exclude`, not both. Repeat the selection on every command;
without a selector, all services are selected.

## Options And Status

```bash
bash run/build.sh --only web --jobs 1
bash run/build.sh --only ui --npmrc "$HOME/.npmrc"
bash run/build.sh --source hrbc1 --release 9-3-0

bash run/build.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/deploy.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/restore.sh --only web --env-file "$HOME/my-hrbc.env"

bash run/feature-env.sh status
bash run/feature-env.sh status --only web
bash run/feature-env.sh stop --exclude ui
```

## Clean Up Local Images

Built images and build snapshots (`.feature-env/builds/<id>/`) accumulate on disk.
`clean` removes local images not currently deployed or recorded as the latest
build, and deletes old build snapshot folders beyond `--keep-builds` (default 2):

```bash
bash run/clean.sh --dry-run
bash run/clean.sh
bash run/clean.sh --only web --keep-builds 1
```

## VS Code Tasks

Open this folder in VS Code and use Terminal > Run Task to run `build`, `deploy`,
`restore`, `status`, `stop`, and `clean` (preview/remove) without typing commands;
each prompts for an optional `--only` target.

Share the GitHub repository only. Your `.env`, state, logs and source snapshots
must stay private and uncommitted. Keep existing restore state.