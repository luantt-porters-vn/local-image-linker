# Run Local Images

Build your local HRBC source (`ui`, `openapi-proxy`, `hrbc`, `api-client-privateapi`)
into Docker images and swap them into an **already-running** HRBC cluster, so you can
test multi-repo changes together without leaving Dev Containers running.

This tool does not create a cluster. Each Build does a real `docker build` from a
snapshot of your working tree; only PHP under `hrbc/product` can additionally be
synced live (see [Live PHP sync](#live-php-sync-web-only)). It complements the Dev
Container workflow; it does not replace it for day-to-day coding.

## 1. Requirements

- Linux/WSL, Bash, Python 3.9+, Git
- Docker with BuildKit, and Compose supporting `up --wait` and `config --format json`
- An existing HRBC cluster already started (e.g. the "Start Cluster" VS Code task)
- Private registry access, and a container mounting `devcontainer-settings`
- VS Code, with this repository (`local-image-linker`) opened as a folder

If you already have **Docker Desktop**, nothing to install there.

Checking **inside your WSL distro** (or Windows if you store your local repos in here), is
Git and Python, since `feature-env.py` runs there via Bash:

```bash
git --version           # any recent version works
python3 --version       # needs 3.9+
```

If either is missing, install it in WSL (Debian/Ubuntu):

```bash
sudo apt-get update && sudo apt-get install -y git python3
```

No `pip install` is needed — the script only uses Python's standard library.

### UI and openapi-proxy sources: local checkout, Docker volume or container

For a repo stored at the root of the `hrbc-ui` named volume, configure:

```dotenv
FEATURE_ENV_UI_REPO="volume://hrbc-ui"
```

Or set `localImageLinker.repos.ui` to `volume://hrbc-ui` in the extension.
If proxy also uses a named volume, use `volume://privateapi-web-proxy` for
`FEATURE_ENV_PROXY_REPO` / `localImageLinker.repos.openapiProxy`.
With `type=bind`, use the host checkout path instead.
Find actual volume names with `docker volume ls`.

The path after the volume name is relative to the **volume root**, not the
Dev Container's mount destination. For your UI workspace mount, the checkout
is at the volume root: use `volume://hrbc-ui`, without `/workspaces/hrbc-ui-react`.
If a volume contains multiple folders, use e.g. `volume://my-volume/ui`.

Build checks the volume exists, creates a temporary helper using the selected
service's saved cluster runtime image (which must be available locally), and mounts
the volume read-only with `volume-nocopy`. The helper is never started and has no
network access. After copying the checkout, the helper is removed; the source
volume remains untouched. This works even after the original Dev Container has
been removed. Missing volumes fail rather than being intentionally created.
No helper image is downloaded.

No Dev Container configuration changes are required. Each repo setting accepts
an absolute host/WSL checkout path or `docker://<container-name-or-id>/<repo-path>`.
Container sources work with both named volumes and bind mounts.

For example, in `src/.env` (replace container names with your own):

```dotenv
FEATURE_ENV_UI_REPO="docker://my-ui-devcontainer/workspaces/hrbc-ui-react"
FEATURE_ENV_PROXY_REPO="docker://my-proxy-devcontainer/workspaces/privateapi-web-proxy"
```

In VS Code extension settings, use the same values for
`localImageLinker.repos.ui` and `localImageLinker.repos.openapiProxy`.
Find container names with `docker ps -a --format '{{.Names}}'`.
You can mix sources: UI in a named volume and proxy at a local host path works too.
Use the container containing the checkout you actually edit, not the cluster's
application container. Docker must be connected to that container's daemon.

Build copies the checkout from the running or stopped container into a private
temporary directory, then snapshots tracked and non-ignored untracked files.
Saved uncommitted edits and deletions are reflected; unsaved editor buffers are not.
Avoid editing during the copy if you need a consistent snapshot.
The same exclusions apply as for local builds: Git metadata, credentials,
dependencies and generated output are excluded from the build context.
The temporary copy is removed on completion or failure. The initial copy includes
the entire checkout (including dependencies), so large checkouts need extra disk
space and can take longer. Nothing is copied back into your repo or container.

The container must still exist; removed containers cannot be read using `docker://`. Use `volume://` to read
a surviving named volume directly. Container and volume sources require a standalone checkout with its own `.git`
directory; linked Git worktrees are not supported. `paths` checks the source
syntax; Build checks that the container and checkout are accessible.
`api-client-privateapi` remains a required local checkout for UI builds, and
`hrbc` continues to use a local checkout.

## 2. One-Time Setup

1. Create `.env` in this folder (`local-image-linker/src`) with the absolute path to
   each of your local checkouts:

   ```dotenv
   FEATURE_ENV_UI_REPO="$HOME/work/ui"
   FEATURE_ENV_PROXY_REPO="$HOME/work/openapi-proxy"
   FEATURE_ENV_HRBC_REPO="$HOME/work/hrbc"
   FEATURE_ENV_CLIENT_REPO="$HOME/work/api-client-privateapi"
   FEATURE_ENV_STATE_DIR=".feature-env"
   FEATURE_ENV_RELEASE="auto"
   ```

2. Make sure your HRBC cluster is already running (start it the same way you
   would for Dev Container work). This tool only replaces containers in an
   existing cluster; it never creates one.

If you move this folder after your first deploy, set `FEATURE_ENV_STATE_DIR` to
the absolute path of your existing state directory instead of starting over —
relative paths resolve beside `.env` and change meaning after a move. The saved
state holds the original images needed for **Restore**; do not delete it.

## 3. Using The VS Code Tasks

Open the **Command Palette ▸ Tasks: Run Task**, then pick one of the tasks below.
Most tasks prompt with a dropdown for an optional target (`ui`, `proxy`, `api`,
`web`, `hrbc`, `api-client-privateapi`); leave it blank to select all services.

| Task | What it does |
| --- | --- |
| **Local Image Linker: Build** | Snapshots your working trees and runs `docker build` for the selected target(s). Does not deploy anything yet. |
| **Local Image Linker: Deploy** | Swaps the selected running containers to the last built local images and waits for their health checks. |
| **Local Image Linker: Restore** | Puts the selected services back on their original saved images. Database data is untouched. |
| **Local Image Linker: Status** | Shows each selected service's container health and which image/commit it's currently running. |
| **Local Image Linker: Sync Web (live)** | After deploying `web`, copies saved changes under `hrbc/product` into the running container every second until stopped. |
| **Local Image Linker: Stop** | Stops the selected containers without changing their configured image. |
| **Local Image Linker: Clean Images (Preview)** | Lists unused local images and stale build snapshots without removing anything. |
| **Local Image Linker: Clean Images (Remove)** | Actually removes those unused local images and stale build snapshots. |

### Everyday workflow

1. Edit code as usual in `ui`, `openapi-proxy`, `hrbc`, or `api-client-privateapi`
   (committed or just saved on disk — both are picked up).
2. If a Dev Container is currently network-connected to a service you're about to
   replace, disconnect it first (its existing network-disconnect task) — Deploy
   refuses to proceed while another container still holds that DNS alias.
3. Run task **Local Image Linker: Build**, choosing your target — the first build ever
   automatically captures the cluster's original images as a restore baseline. It
   does not replace any container by itself.
4. Run task **Local Image Linker: Deploy** with the same target to swap it into the
   cluster, then test as normal.
5. Repeat steps 1, 3, 4 every time you want your latest changes reflected — each run
   is a fresh snapshot and a fresh `docker build`. For PHP-only edits, see below.
6. When finished, run task **Local Image Linker: Restore** with the same target to put
   the original image back.

Deploy and Restore restart only the routers affected by the selected applications:
`api` restarts `hrbcprivateapicore`; `ui`, `proxy`, or `web` restarts `hrbcweblb`.
Selecting both groups restarts both routers.

### Live PHP sync (web only)

After **Deploy** of `web`, run task **Local Image Linker: Sync Web (live)** (or
`bash run/feature-env.sh sync --only web [--interval SECONDS]`). It first copies
anything saved under `hrbc/product` since that build, then keeps copying saved
changes and deletions into the running `hrbcproductweb` container. PHP runs without
opcache, so the next request uses the new code; no restart or rebuild is needed.

- Uses the same file selection as Build (tracked and non-ignored files, no credentials).
- `static_source` changes are not synced; they need Build & Deploy (Ant build).
- Status still shows the built image; synced edits live only in that container.
  The next Deploy or Restore recreates it and discards them.
- Sync holds the command lock: stop it (Ctrl+C) before Build/Deploy/Restore.
- It refuses to start unless `web` runs the image this tool deployed and that
  build's snapshot still exists (Clean may remove it; then Build & Deploy again).

### What you'll see during a build

The task's terminal updates in place, one row per image, showing status, elapsed
time, and the current Docker step. Full Docker output is saved to
`builds/<build-id>/<target>.log`; a failure message includes the path to the
relevant log.

## 4. Troubleshooting

- **"Another feature-env command is running"** — a previous Build/Deploy/Restore
  task is still holding the lock file; wait for it to finish or check for a
  stuck process.
- **"... still redirects ..."** during Deploy — a Dev Container is still
  network-connected to that service; disconnect it first, then retry.
- **"No prepared cluster state"** — run Build successfully at least once before
  Deploy/Restore/Status; if you moved the folder, set `FEATURE_ENV_STATE_DIR`
  instead of starting over.
- **Build fails** — fix it before running Deploy; the previous working images
  stay deployed and the log path is printed in the error.



## Extra: Running The Scripts Directly

The tasks above just call the shell launchers in `run/`; you can run the same
commands yourself from a terminal, from this folder (`local-image-linker/src`).

`run/feature-env.sh` is the shared launcher for the Python implementation. The
build, deploy, and restore scripts call it with their command; use it directly
for `paths`, `status`, `stop`, and `clean`.

### Selecting services

Targets: `ui`, `openapi-proxy` (or `proxy`), `api`, `web`, `hrbc` (alias for `api`
+ `web`), `api-client-privateapi` (alias for `ui`, since it's bundled into the UI
build). Use either `--only` or `--exclude`, never both. Without a selector, all
services are selected. Repeat your selection on every command — it is not
remembered between `build`, `deploy`, and `restore`.

```bash
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

# Restore without touching excluded services
bash run/restore.sh --exclude ui
```

### Command reference

| Command | What it does |
| --- | --- |
| `bash run/build.sh [--only/--exclude ...]` | Snapshot working trees and `docker build` each selected target. Does not deploy. |
| `bash run/deploy.sh [--only/--exclude ...]` | Swap selected running containers to the last built local images and wait for health checks. |
| `bash run/restore.sh [--only/--exclude ...]` | Put selected services back on their original saved images. Database data is untouched. |
| `bash run/feature-env.sh status [--only/--exclude ...]` | Show each selected service's container health and which image/commit it's running. |
| `bash run/feature-env.sh stop [--only/--exclude ...]` | Stop selected containers without changing their configured image. |
| `bash run/clean.sh [--dry-run] [--only/--exclude ...] [--keep-builds N]` | Remove unused local images and old build snapshots. |
| `bash run/feature-env.sh paths [--only/--exclude ...]` | Print the repository path resolved for each target, without touching Docker. |
| `bash run/feature-env.sh sync --only web [--interval S]` | Copy saved `hrbc/product` changes into the deployed web container until Ctrl+C. |

### Useful options

```bash
bash run/build.sh --only web --jobs 1                 # limit concurrent builds (1-4, default 2)
bash run/build.sh --only ui --npmrc "$HOME/.npmrc"     # mount private npm credentials for the build
bash run/build.sh --source hrbc1 --release 9-3-0       # target a differently named/versioned cluster

bash run/build.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/deploy.sh --only web --env-file "$HOME/my-hrbc.env"
bash run/restore.sh --only web --env-file "$HOME/my-hrbc.env"
```

`--release` overrides `FEATURE_ENV_RELEASE` from `.env`. Leave it blank or use
`auto` to detect the running cluster's release from its published office image.
An explicit version must match the running cluster; it does not upgrade the cluster.

After starting a new cluster release with its published application images, run
Build again. The tool backs up the old baseline, captures the upgraded cluster,
and invalidates old build selections. Build each target before deploying it.
If old local images are still running, restart the upgraded cluster with its
published application images first; the tool will not capture local images as
restore images. Existing builds without recorded release metadata also require
a rebuild before Deploy. Restore refuses to use a baseline from a different release.
