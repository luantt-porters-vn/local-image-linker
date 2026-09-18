# Run Local Images — User Manual

Build your local HRBC source (`ui`, `openapi-proxy`, `hrbc`, `api-client-privateapi`)
into Docker images and swap them into an **already-running** HRBC cluster, so you can
test multi-repo changes together without leaving Dev Containers running.

This tool does not create a cluster and does not give you live reload — it does a real
`docker build` from a snapshot of your working tree each time you ask it to. It
complements the Dev Container workflow; it does not replace it for day-to-day coding.

## 1. Requirements

- Linux/WSL, Bash, Python 3.9+, Git
- Docker with BuildKit, and Compose supporting `up --wait` and `config --format json`
- An existing HRBC cluster already started (e.g. the "Start Cluster" VS Code task)
- Private registry access, and a container mounting `devcontainer-settings`

Shell launchers are in `run/`; Dockerfiles are in `docker/`. Everything resolves
paths from its own location, so you can run these commands from any directory,
but this manual assumes `cd` into this folder (`images-linker/src`) first.

## 2. One-Time Setup

1. Create `.env` in this folder with the absolute path to each of your local checkouts:

   ```dotenv
   FEATURE_ENV_UI_REPO="$HOME/work/ui"
   FEATURE_ENV_PROXY_REPO="$HOME/work/openapi-proxy"
   FEATURE_ENV_HRBC_REPO="$HOME/work/hrbc"
   FEATURE_ENV_CLIENT_REPO="$HOME/work/api-client-privateapi"
   FEATURE_ENV_STATE_DIR=".feature-env"
   ```

2. Check the paths resolve correctly:

   ```bash
   bash run/feature-env.sh paths
   ```

3. Make sure your HRBC cluster is already running (start it the same way you would
   for Dev Container work). This tool only replaces containers in an existing cluster.

If you move this folder after your first deploy, set `FEATURE_ENV_STATE_DIR` to the
absolute path of your existing state directory instead of re-preparing — relative
paths resolve beside `.env` and change meaning after a move. The saved state holds
the original images needed for `restore`; do not delete it.

## 3. Everyday Workflow

1. Edit code as usual in `ui`, `openapi-proxy`, `hrbc`, or `api-client-privateapi`
   (committed or just saved on disk — both are picked up).
2. If a Dev Container is currently network-connected to a service you're about to
   replace, disconnect it first (its existing network-disconnect task) — deploy
   refuses to proceed while another container still holds that DNS alias.
3. Build, then deploy:

   ```bash
   bash run/build.sh --only ui
   bash run/deploy.sh --only ui
   ```

   The first build ever automatically captures the cluster's original images as a
   restore baseline; it does not replace any container by itself. `build` only
   produces an image — you always need `deploy` afterward to swap it in.
4. Test against the cluster as normal.
5. When finished, put the original image back:

   ```bash
   bash run/restore.sh --only ui
   ```

Repeat step 3 every time you want your latest changes reflected — there is no
watch mode; each run is a fresh snapshot and a fresh `docker build`.

### What you'll see during a build

Progress updates in place in an interactive terminal, one row per image, showing
status, elapsed time, and the current Docker step. When output is redirected (or
`TERM=dumb`), it instead prints status changes and a progress line every 15
seconds. Full Docker output is saved to `builds/<build-id>/<target>.log`; a
failure message includes the path to the relevant log.

## 4. Selecting Services

Targets: `ui`, `openapi-proxy` (or `proxy`), `api`, `web`, `hrbc` (alias for `api`
+ `web`), `api-client-privateapi` (alias for `ui`, since it's bundled into the UI
build). Use either `--only` or `--exclude`, never both. Without a selector, all
services are selected. Repeat your selection on every command — it is not
remembered between `build`, `deploy`, and `restore`.

```bash
# Only selected services
bash run/build.sh --only web
bash run/deploy.sh --only web

bash run/build.sh --only ui openapi-proxy
bash run/deploy.sh --only ui openapi-proxy

# Restore selected services when finished
bash run/restore.sh --only web
bash run/restore.sh --only ui openapi-proxy

# Leave some services to Dev Containers instead
bash run/build.sh --exclude ui
bash run/deploy.sh --exclude ui

bash run/build.sh --exclude ui openapi-proxy
bash run/deploy.sh --exclude ui openapi-proxy

# Restore without touching excluded services
bash run/restore.sh --exclude ui
bash run/restore.sh --exclude ui openapi-proxy
```

## 5. Command Reference

| Command | What it does |
| --- | --- |
| `bash run/build.sh [--only/--exclude ...]` | Snapshot working trees and `docker build` each selected target. Does not deploy. |
| `bash run/deploy.sh [--only/--exclude ...]` | Swap selected running containers to the last built local images and wait for health checks. |
| `bash run/restore.sh [--only/--exclude ...]` | Put selected services back on their original saved images. Database data is untouched. |
| `bash run/feature-env.sh status [--only/--exclude ...]` | Show each selected service's container health and which image/commit it's running. |
| `bash run/feature-env.sh stop [--only/--exclude ...]` | Stop selected containers without changing their configured image. |
| `bash run/clean.sh [--dry-run] [--only/--exclude ...] [--keep-builds N]` | Remove unused local images and old build snapshots (see below). |
| `bash run/feature-env.sh paths [--only/--exclude ...]` | Print the repository path resolved for each target, without touching Docker. |

### Useful options

```bash
bash run/build.sh --only web --jobs 1                 # limit concurrent builds (1-4, default 2)
bash run/build.sh --only ui --npmrc "$HOME/.npmrc"     # mount private npm credentials for the build
bash run/build.sh --source hrbc1 --release 9-3-0       # target a differently named/versioned cluster

bash run/build.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/deploy.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/restore.sh --only web --env-file "$HOME/my-hrbc.env"
```

## 6. Clean Up Local Images

Built images and build snapshots (`.feature-env/builds/<id>/`) accumulate on disk
over time. `clean` removes local images that are neither currently deployed nor
recorded as the latest build, and deletes old build snapshot folders beyond
`--keep-builds` (default 2 per target selection):

```bash
bash run/clean.sh --dry-run       # preview what would be removed
bash run/clean.sh                 # actually remove it
bash run/clean.sh --only web --keep-builds 1
```

## 7. VS Code Tasks

Open this folder (`images-linker`) in VS Code and use **Terminal ▸ Run Task** to
run `build`, `deploy`, `restore`, `status`, `stop`, and `clean` (preview/remove)
without typing commands. Each task prompts for an optional `--only` target from a
dropdown; leave it blank to target all services.

## 8. Troubleshooting

- **"Another feature-env command is running"** — a previous `build`/`deploy`/
  `restore` is still holding the lock file; wait for it to finish or check for a
  stuck process.
- **"... still redirects ..."** during deploy — a Dev Container is still
  network-connected to that service; disconnect it first, then retry.
- **"No prepared cluster state"** — run `build` successfully at least once before
  `deploy`/`restore`/`status`; if you moved the folder, set `FEATURE_ENV_STATE_DIR`
  instead of re-preparing.
- **Build fails** — fix it before running `deploy`; the previous working images
  stay deployed and the log path is printed in the error.

## 9. Privacy

Share the GitHub repository only. Your `.env`, state, logs and source snapshots
must stay private and uncommitted. Keep existing restore state.