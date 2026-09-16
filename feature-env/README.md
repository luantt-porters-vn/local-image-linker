# Run Local Images

Run from this folder. Requires Linux/WSL, Bash, Python 3.9+, Git, Docker BuildKit,
Compose with `up --wait` and `config --format json`, an existing HRBC cluster,
private registry access, and a container mounting `devcontainer-settings`.

Shell launchers are in `run/`; Dockerfiles are in `docker/`. Run the commands below
from this folder, leaving `.env` and existing state in their current locations.

`run/feature-env.sh` is the shared launcher for the Python implementation.
The build, deploy and restore scripts call it with their command; use it directly
for `paths`, `status`, `stop` and `package`. It does not start another service.

## Configure

Edit the included `.env` with your local repository roots:

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

## Share

```bash
bash run/feature-env.sh package --output ../feature-env.zip
```

Use a new output filename. The ZIP includes blank configuration; your configured
`.env`, state, logs and source snapshots stay private. Keep existing restore state.