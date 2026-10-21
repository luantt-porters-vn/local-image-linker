# Run Local Images

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
| **Local Image Linker: Build & Share** | Builds selected images, exports a bundle, and temporarily shares it over pinned HTTPS on the LAN. |
| **Local Image Linker: Share Last Build** | Shares selected existing builds without rebuilding. |
| **Local Image Linker: Pull Shared** | Prompts for a share link, validates and imports its images without building or deploying. |
| **Local Image Linker: Stop Sharing** | Ends the active share in this state directory and requests temporary bundle cleanup. |
| **Local Image Linker: Deploy** | Swaps the selected running containers to the last built local images and waits for their health checks. |
| **Local Image Linker: Restore** | Puts the selected services back on their original saved images. Database data is untouched. |
| **Local Image Linker: Status** | Shows each selected service's container health and which image/commit it's currently running. |
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
5. Repeat steps 1, 3, 4 every time you want your latest changes reflected — there
   is no watch mode; each run is a fresh snapshot and a fresh `docker build`.
6. When finished, run task **Local Image Linker: Restore** with the same target to put
   the original image back.

Deploy and Restore restart only the routers affected by the selected applications:
`api` restarts `hrbcprivateapicore`; `ui`, `proxy`, or `web` restarts `hrbcweblb`.
Selecting both groups restarts both routers.

### What you'll see during a build

All tasks use consistent headers, named stages, status labels and completion
timings. Interactive terminals use color and in-place progress; captured logs
remain plain text. Set `NO_COLOR=1` to disable color. Metadata wraps to the
terminal width; share links stay intact so the complete secret link can be copied.
Docker diagnostic output remains visible when commands run or fail.

The task's terminal updates in place, one row per image, showing status, elapsed
time, and the current Docker step. Full Docker output is saved to
`builds/<build-id>/<target>.log`; a failure message includes the path to the
relevant log.

### Sharing builds over the LAN

Both computers need the updated linker, Docker, and their own already-running HRBC
cluster. The release and image CPU architecture must match. The receiver does not
need application source repositories or build credentials. Its existing cluster
setup, including `devcontainer-settings`, is still needed to capture a local
Restore baseline. CLI users need their usual `src/.env`; receivers can omit all
repository settings and leave release detection automatic.

1. Giver: run **Build & Share**, select services, and keep the task running.
   Use **Share Last Build** to avoid rebuilding an existing successful selection.
   In WSL NAT mode, approve the Windows administrator (UAC) prompt using the
   same Windows account. The task configures temporary LAN access automatically.
2. Send the complete displayed link to the coworker through a trusted channel.
   Its token and certificate fingerprint are secret; do not post it publicly.
3. Receiver: run **Pull Shared** and paste the link into its terminal prompt.
   All services in that share are imported. No source checkout is required.
4. Receiver: run **Deploy**, selecting only the imported services. Pull never
   deploys automatically. **Restore** uses the receiver's original cluster images,
   not the giver's configuration. On first pull, the local baseline is captured.
5. Giver: run **Stop Sharing**, press Ctrl+C in the sharing terminal, or let it
   expire (60 minutes after export by default). Normal shutdown removes the
   temporary image bundle and TLS key. Keep the computer awake until pulls finish.

Only an image archive and selected source provenance are exposed. No file browser,
Docker socket, source snapshots, cluster settings, or database export is served.
Images themselves can still contain code or embedded secrets: only accept images
from trusted coworkers. A pinned certificate protects the transfer, not the safety
of the software inside an image. No cloud upload or Docker registry is used.

Sharing binds to one private IPv4 interface and accepts peers from its subnet.
With multiple eligible interfaces, the task asks which address to use. This is
LAN restriction, not proof that peers use the same Wi-Fi name. VPNs, routers and
NAT can change reachability or hide peer addresses; the host firewall must also
restrict access. Never expose the sharing port through public router forwarding.

TLS uses host OpenSSL when available. Otherwise Docker obtains an
`alpine/openssl:latest` helper automatically; only the temporary certificate
directory is mounted and the helper runs without network access. That first
helper download requires registry access, but no manual package installation.

The task reports the selected interface, likely WSL/container exposure issues,
authorized download starts, transfer progress, compatibility failures and checksum
failures. Native Linux sharing does not change privileged network settings.

For WSL NAT, the giver task detects the Windows LAN address and requests one
explicit Windows UAC approval. An elevated helper creates a TCP port-forward and
an inbound firewall rule restricted to that address, port, interface and its
actual LAN subnet. It does not disable the firewall, change the network profile,
install a service, or configure public router forwarding. The displayed share
link already uses the Windows LAN address; receivers just paste it as shown.
If multiple Windows networks are active, the task asks which LAN to use.

Stop Sharing, Ctrl+C and expiry request removal of only the session's own rules,
without a second UAC prompt. A Windows-side watchdog removes them after about
30 seconds without a changing WSL heartbeat, covering unexpected task exits.
Cleanup is confirmed before temporary helper files are removed. If cleanup
cannot be confirmed, the task retains recovery records in the Windows temporary
directory and reports the owned rule and forwarding endpoint. Windows crashes,
power loss, security-policy restrictions or forcibly killing the elevated helper
can still leave persistent rules requiring manual removal. Existing/manual rules
are never overwritten or deleted: remove an old manual forwarding rule yourself,
or select another sharing port. WSL mirrored mode and generic containers do not
use this NAT helper and may still require host/firewall configuration.

| Symptom | What to check |
| --- | --- |
| Cannot bind / port in use | Stop an existing share or select a different sharing port. |
| Receiver connection timeout | Confirm the giver task is running, both devices can reach the LAN, and the giver's private-network firewall allows the port. Guest Wi-Fi/client isolation can prevent peer connections. A timeout cannot uniquely identify the cause. |
| Giver runs inside WSL NAT | Approve the task's Windows UAC prompt. Use its displayed Windows LAN link unchanged. Windows PowerShell, WSL interoperability, Windows-drive mounting and a running IP Helper service are required. |
| Windows forwarding setup fails | Check the helper's error. Cancelled UAC can be retried; an old manual forwarding rule must be removed manually or a different sharing port selected. The task does not take ownership of existing rules. |
| Giver runs inside a container / WSL mirrored mode | Use a LAN-reachable interface and verify host/firewall exposure. The automatic Windows NAT helper does not configure these modes. |
| Certificate fingerprint mismatch | Stop and obtain the current link directly from the giver. Never bypass verification. |
| Link unavailable / expired | Ask the giver for the link from the currently running share. Each share has a fresh token and certificate. |
| Release or architecture mismatch | Start a matching HRBC release or request a build for the receiver's Docker architecture. No automatic emulation or cross-release deployment. |
| Not enough storage / Docker load failed | Free staging disk space and check Docker daemon/Desktop storage limits. Images may consume additional daemon storage. |

CLI equivalents (from `src`):

```bash
bash run/feature-env.sh build-share --only ui proxy
bash run/feature-env.sh share --only ui proxy
bash run/feature-env.sh pull-shared
bash run/feature-env.sh stop-sharing

# Optional giver overrides; defaults require no sharing configuration.
bash run/feature-env.sh share --only ui --share-address 192.168.1.20 --share-port 8443 --share-minutes 30
```

Sharing runs in the foreground but releases the cluster lock after export, so
Deploy/Status/Restore can run while a share is active. Pull takes the normal
cluster lock while importing. A failed download or validation leaves deployment
selections unchanged; images are registered only after a successful Docker load.
Forced process termination or a system crash can leave `sharing-*`/`pull-*`
temporary directories in the state directory; remove these only after confirming
no sharing/import process is using them. Never delete the restore state.

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
