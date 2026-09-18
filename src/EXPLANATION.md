# images-linker: Repository Explanation

This guide explains the checked-out source as inspected on 2026-09-16. Code blocks marked **Source** are exact excerpts, including the repository's original comments; explanations appear below them. Directory trees, flow maps, and explicitly labeled examples are explanatory illustrations.

The guide is based on reading the implementation. No build, deployment, restore, or Docker health check was executed while writing it.

## 1. High-Level Overview

`images-linker` packages saved local HRBC source code into Docker images and switches selected services in an existing local Docker Compose environment to those images. It records the original application image references and configuration so the services can later be restored. The repository is a command-line orchestration tool; the business applications it builds live in other repositories.

The central connection between application names, Compose services, and source repositories is defined here:

**Source:** [src/feature-env.py](src/feature-env.py), lines 22–30.

```python
BUNDLE = Path(__file__).resolve().parent
STATE = BUNDLE / '.feature-env'
SERVICES = {'ui': 'hrbcui', 'proxy': 'hrbcprivateapiproxy', 'api': 'hrbcprivateapicoreapp', 'web': 'hrbcproductweb'}
# Each key gets its own snapshot/context/image, even 'api' and 'web' which both build from the 'hrbc' repo.
REPOS = {'ui': 'ui', 'proxy': 'openapi-proxy', 'api': 'hrbc', 'web': 'hrbc', 'client': 'api-client-privateapi'}
REPO_ENV = {'ui': 'FEATURE_ENV_UI_REPO', 'openapi-proxy': 'FEATURE_ENV_PROXY_REPO',
            'hrbc': 'FEATURE_ENV_HRBC_REPO', 'api-client-privateapi': 'FEATURE_ENV_CLIENT_REPO'}
TARGETS = {key: (key,) for key in SERVICES}
TARGETS.update({'openapi-proxy': ('proxy',), 'hrbc': ('api', 'web'), 'api-client-privateapi': ('ui',)})
```

- `SERVICES` translates a short command target such as `ui` into the actual Compose service `hrbcui`.
- `REPOS` identifies the source repository for each image. Both `api` and `web` use `hrbc`, but produce separate images.
- `TARGETS` adds convenient aliases: `hrbc` selects both Java API and PHP web images; `api-client-privateapi` selects the UI image that contains the library.
- `BUNDLE` is the absolute `src/` directory. `STATE` initially points to `src/.feature-env`, and configuration can change it.

### Core technology stack

| Technology | Actual role in this repository |
| --- | --- |
| Bash | Four small command launchers in `src/run/` |
| Python 3.9+ standard library | CLI parsing, filesystem operations, subprocesses, JSON state, locking, and concurrent builds |
| Docker BuildKit | Builds images using multi-stage Dockerfiles, cache mounts, and an optional npm secret mount |
| Docker Compose CLI | Reads the existing environment configuration and replaces selected application containers |
| Git | Enumerates source files and records branch/commit information; provides version metadata for Java builds |
| Node.js and npm | Build the UI/client with Node 16.20.2 and proxy with Node 22.22.0, using the pinned base image tags |
| JDK 11, Gradle wrapper, Tomcat | Compile Java modules and package/deploy `PrivateAPI.war` |
| Ant, PHP, Apache, nginx | Build legacy static assets; run the PHP website and serve the compiled frontend |

The Python program has no third-party Python imports or package installation step. Application dependency manifests and build tools such as the Gradle wrappers come from the copied source repositories. Docker base images and npm dependencies use the private registries named in the Dockerfiles.

## 2. Directory & Module Blueprint

```text
images-linker/
├── REPOSITORY_EXPLANATION.md      # This guide
└── src/
    ├── README.md                 # Operator commands and configuration example
    ├── .gitignore                # Excludes local configuration/state
    ├── feature-env.py            # Main orchestration implementation
    ├── web-static.conf           # Apache rules added to the web image
    ├── run/
    │   ├── feature-env.sh        # Shared Python launcher
    │   ├── build.sh              # Selects Python's build command
    │   ├── deploy.sh             # Selects Python's up command
    │   └── restore.sh            # Selects Python's restore command
    └── docker/
        ├── ui.Dockerfile
        ├── proxy.Dockerfile
        ├── api.Dockerfile
        └── web.Dockerfile
```

**`src/`** owns orchestration rather than HRBC application logic. `feature-env.py` coordinates external commands, `web-static.conf` supplies web-server configuration, and `README.md` describes how a developer invokes the tool. Local `.env` and generated state normally live here but are not committed source.

**`src/run/`** supplies human-friendly command names. Each launcher delegates immediately; it does not run a separate long-lived server. See the exact shell code in section 4.1.

**`src/docker/`** contains one build recipe per application image. Python selects the Dockerfile using the target key (`ui`, `proxy`, `api`, or `web`) and passes a generated snapshot folder as its build context. The recipes are explained individually in sections 4.10–4.13.

The generated state directory has a different responsibility: remembering configuration and retaining build inputs/logs. Its default layout is:

```text
src/.feature-env.lock             # Lock beside the state directory
src/.feature-env/
├── compose/                      # Copied existing Compose definitions
├── normal.json                   # Saved baseline Compose configuration
├── state.json                    # Project, original image references, daemon identity
├── feature.json                  # Compose overrides pointing to local images
├── build.json                    # Image names and source provenance
├── routing.json                  # Optional web routing override
└── builds/<unique-build-id>/
    ├── ui/                       # Present when UI selected
    ├── client/                   # Included with UI
    ├── proxy/                    # Present when proxy selected
    ├── api/                      # Present when API selected
    ├── web/                      # Present when web selected
    ├── web-static.conf           # Included with web
    └── <target>.log              # Docker build output
```

These are filesystem records, not an application database. This code writes them using `save()`, `snapshot()`, and `build()`, shown below.

## 3. Core Concepts & Architecture

### Architectural pattern: CLI orchestrator with persisted local state

The program is a procedural command-line controller. It reads configuration, calls existing tools, and persists the results needed for later commands. It does not expose HTTP routes or implement MVC controllers, a database access layer, or a message queue.

The dispatch code makes the architecture explicit:

**Source:** [src/feature-env.py](src/feature-env.py), lines 499–513.

```python
    if args.command == 'build':
        build(args, state)
    elif args.command == 'up':
        deploy(state, args.targets)
    elif args.command == 'restore':
        # Reuse the baseline without local image overrides; database contents are untouched.
        check_redirects(state, args.targets)
        run(compose(state, False) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *[SERVICES[key] for key in args.targets]])
        if 'web' in args.targets:
            refresh_web_routing(state)
        run(['docker', 'restart', state['project'] + '-hrbcprivateapicore-1', state['project'] + '-hrbcweblb-1'])
    elif args.command == 'stop':
        run(compose(state, False) + ['stop', *[SERVICES[key] for key in args.targets]])
    else:
        summary(state, args.targets)
```

`build`, `up`, `restore`, `stop`, and `status` are separate operations. In particular, building an image and changing a running container are separate calls.

### Command and data flow

```text
Developer: bash run/build.sh --only ui
    │
    ▼
run/build.sh → run/feature-env.sh → feature-env.py: main()
                                      │
                     .env → targets → saved baseline
                                      │
                               build() → snapshot()
                                      │
                   Docker CLI + docker/ui.Dockerfile
                                      │
                          Docker daemon stores image
                                      │
                         feature.json + build.json

Developer: bash run/deploy.sh --only ui
    │
    ▼
main() → deploy() → compose()
                       │
              normal.json + feature.json
                       │
                  docker compose up
                       │
             selected container → health check → status
```

### Docker vocabulary tied to the code

An **image** is the built application package; a **container** is an instance running that package. A **build context** is the directory of files made available to Docker's `COPY` instructions. A **Compose project** groups related services, containers, networks, and volumes under a name such as `hrbc1`.

The image command is assembled here:

**Source:** [src/feature-env.py](src/feature-env.py), lines 256–270.

```python
    images = {}
    for key in args.targets:
        sha = metadata[key]['sha'][:12]
        images[key] = f'local/{state["project"]}-{key}:feature-{sha}-{build_id}'
    failed = threading.Event()
    def one(key):
        if failed.is_set():
            raise ValueError('Cancelled after another build failed')
        logfile = context / (key + '.log')
        cmd = ['docker', 'build', '--progress=plain', '-f', BUNDLE / 'docker' / (key + '.Dockerfile'), '-t', images[key], '--build-arg', 'RUNTIME_IMAGE=' + state['originals'][SERVICES[key]]]
        for entry in args.add_host:
            cmd += ['--add-host', entry]
        if args.npmrc:
            cmd += ['--secret', 'id=npmrc,src=' + str(Path(args.npmrc).resolve())]
        cmd += [context]
```

- The generated image tag contains a source commit prefix and a unique build ID. The extra build ID distinguishes rebuilds, including different uncommitted source changes at the same commit.
- `-f` selects the recipe; `-t` names the output image.
- `--build-arg RUNTIME_IMAGE=...` supplies the original application's image reference as the final stage's base image.
- The last argument, `context`, determines what `COPY ui/...` and similar paths mean. They refer to snapshot files, not paths relative to the Dockerfile.
- `--secret` optionally exposes npm configuration during the relevant build steps. The script never runs `docker push`; these output images are local to the Docker daemon.

The code combines baseline configuration and local image choices with an override:

**Source:** [src/feature-env.py](src/feature-env.py), lines 324–329.

```python
def compose(state, feature=True):
    """Use the saved baseline, optionally overlaid with successfully built local images."""
    cmd = ['docker', 'compose', '-p', state['project'], '-f', STATE / 'normal.json']
    if feature:
        cmd += ['-f', STATE / 'feature.json']
    return cmd
```

`normal.json` supplies the saved service configuration. Adding `feature.json` replaces selected service image settings while retaining the rest of that configuration. Passing `feature=False` omits that override, which is the central mechanism behind restore.

## 4. Component-by-Component Code Deep-Dive

### 4.1 Shell entry points

**File paths:** `src/run/build.sh`, `src/run/deploy.sh`, `src/run/restore.sh`, `src/run/feature-env.sh`.

**Purpose:** Translate an operator command into the correct Python subcommand and forward its arguments.

**Source:** [src/run/build.sh](src/run/build.sh), lines 1–4.

```bash
#!/usr/bin/env bash
# Build local source images without deploying; forward selectors and build options.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" build "$@"
```

**Source:** [src/run/deploy.sh](src/run/deploy.sh), lines 1–4.

```bash
#!/usr/bin/env bash
# Deploy previously built images; --only and --exclude select application services.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" up "$@"
```

**Source:** [src/run/restore.sh](src/run/restore.sh), lines 1–4.

```bash
#!/usr/bin/env bash
# Restore selected applications to saved normal images, not source or database data.
set -euo pipefail
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" restore "$@"
```

**Source:** [src/run/feature-env.sh](src/run/feature-env.sh), lines 1–5.

```bash
#!/usr/bin/env bash
# Shared entry point: find the Python implementation above run/, regardless of cwd.
set -euo pipefail
# Replace the shell so exit codes and signals reach the caller unchanged.
exec python3 "$(dirname "$(readlink -f "$0")")/../feature-env.py" "$@"
```

**Annotations:**

- `#!/usr/bin/env bash` selects Bash when the script is executed directly.
- `set -euo pipefail` enables failure on failed simple commands, unset variables, and pipeline errors, subject to Bash's usual conditional-command rules.
- `$0` is the current script's path. `readlink -f` resolves it to an absolute path; `dirname` takes its containing directory. This lets the scripts find one another regardless of your current directory.
- `"$@"` forwards every argument separately. For example, `--only ui --jobs 1` reaches Python unchanged.
- `exec` replaces the shell process with the next program. Signals and exit codes reach the caller without another waiting wrapper process.
- `deploy.sh` deliberately passes `up`, which is the command name accepted by `main()`.

### 4.2 CLI initialization, configuration, and locking

**File path:** `src/feature-env.py`.

**Purpose:** `main()` establishes the configuration and dispatches exactly one operation.

**Source:** [src/feature-env.py](src/feature-env.py), lines 440–478.

```python
def main():
    """Parse commands, load private configuration, lock state and enforce daemon identity."""
    global STATE
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'paths', 'build', 'up', 'restore', 'stop', 'status'])
    parser.add_argument('--env-file', type=Path, help='Configuration file; defaults to .env beside this script')
    parser.add_argument('--source', default='hrbc1')
    parser.add_argument('--release', default='9-3-0')
    parser.add_argument('--settings-container', help='Existing stopped container mounting devcontainer-settings')
    parser.add_argument('--repos', help='Fallback parent of the four repositories; FEATURE_ENV_*_REPO variables override individual paths')
    parser.add_argument('--add-host', action='append', default=[], help='Optional build-only hostname:IP mapping for VPN/Docker DNS')
    parser.add_argument('--npmrc', help='Private npm config mounted as a BuildKit secret')
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=2)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--only', nargs='+', choices=TARGETS, help='Build/manage only these repositories or runtime targets')
    selection.add_argument('--exclude', nargs='+', choices=TARGETS, help='Leave these repositories or runtime targets to Dev Containers')
    args = parser.parse_args()
    env_file = (args.env_file or BUNDLE / '.env').expanduser().resolve()
    if not env_file.is_file():
        raise ValueError(f'Environment file not found: {env_file}. Create and edit it using the README configuration before running this command.')
    load_environment(env_file)
    STATE = Path(os.environ.get('FEATURE_ENV_STATE_DIR', str(BUNDLE / '.feature-env'))).expanduser().resolve()
    print(f'Configuration: {env_file if env_file.is_file() else "process environment/defaults"}\nState: {STATE}', flush=True)
    if args.command == 'prepare' and (args.only or args.exclude):
        parser.error('prepare captures the whole cluster; selection applies to paths/build/up/restore/stop/status')
    args.targets = select_targets(args.only, args.exclude)
    if args.command == 'paths':
        state = json.loads((STATE / 'state.json').read_text()) if (STATE / 'state.json').exists() else {}
        paths = repository_paths(state, args.targets, args.repos)
        print(json.dumps({key: {'environment_variable': REPO_ENV[REPOS[key]], 'path': str(repo)}
                          for key, repo in paths.items()}, indent=2))
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    lock = STATE.with_name(STATE.name + '.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('Another feature-env command is running')
```

**Annotations:**

1. `os.umask(0o077)` restricts permissions of newly created files/directories for other users. This matters because captured environment values can contain private configuration.
2. `argparse` defines the accepted commands and flags. `--source` defaults to `hrbc1`; `--release` defaults to `9-3-0`; `--jobs` defaults to two and accepts one through four.
3. The mutually exclusive group prevents combining `--only` and `--exclude`.
4. A `.env` file is required even when environment variables are already exported. An explicit `--env-file` changes which file is used.
5. `paths` resolves repositories and returns before acquiring the operational lock or inspecting Docker state.
6. The `.lock` file sits beside the state directory. `LOCK_EX | LOCK_NB` requests an exclusive, nonblocking lock: another running command causes a readable error instead of overlapping changes. The OS releases the lock when the process exits.

The remainder of initialization handles the first build and environment identity:

**Source:** [src/feature-env.py](src/feature-env.py), lines 479–498.

```python
    if args.command == 'prepare':
        prepare(args)
        return
    if args.command == 'build' and not (STATE / 'state.json').exists():
        repository_paths({}, args.targets, args.repos)
        if (STATE / 'normal.json').exists():
            raise ValueError('Incomplete preparation: normal.json exists without state.json; review before retrying')
        print(f'First build: preparing existing cluster {args.source} (release {args.release}). No containers will be replaced.', flush=True)
        prepare(args)
    if not (STATE / 'state.json').is_file():
        raise ValueError(
            f'No prepared cluster state in {STATE}. Run build successfully before deploying. '
            'If you moved this folder, set FEATURE_ENV_STATE_DIR in your .env '
            'to the existing state directory instead of preparing again.'
        )
    state = json.loads((STATE / 'state.json').read_text())
    if state.get('daemon') and output(['docker', 'info', '--format', '{{.ID}}']).strip() != state['daemon']:
        raise ValueError('Docker daemon changed since preparation')
    if state['project'] != state['source']:
        raise ValueError('State targets a separate project; prepare existing-cluster state first')
```

**Source:** [src/feature-env.py](src/feature-env.py), lines 515–520.

```python
if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'feature-env: {exc}', file=sys.stderr)
        sys.exit(1)
```

- `prepare` explicitly captures the baseline; the first `build` does the same automatically if `state.json` does not exist.
- A leftover `normal.json` without `state.json` is treated as incomplete preparation rather than silently overwritten.
- The saved Docker daemon ID is compared with the current daemon. A daemon is the background Docker engine that owns the containers and images; this check catches switching to a different engine after preparation.
- The tool operates on the existing project: saved `project` and `source` must match.
- The final `try/except` converts common validation, filesystem, and failed-command errors into `feature-env: ...` messages and exit status `1`.

### 4.3 Reading `.env`, choosing targets, and locating repositories

**File path:** `src/feature-env.py`.

**Purpose:** `load_environment()`, `select_targets()`, and `repository_paths()` convert user configuration into validated source locations.

**Source:** [src/feature-env.py](src/feature-env.py), lines 32–62.

```python
def load_environment(path):
    """Parse allowed path settings as data; exported environment values take precedence."""
    allowed = set(REPO_ENV.values()) | {'FEATURE_ENV_STATE_DIR'}
    values = {}
    if path.is_file():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            if len(tokens) != 1 or '=' not in tokens[0]:
                raise ValueError(f'{path.name}:{number}: expected KEY="value"')
            key, value = tokens[0].split('=', 1)
            if key not in allowed:
                raise ValueError(f'{path.name}:{number}: unsupported setting {key}')
            if key in values:
                raise ValueError(f'{path.name}:{number}: duplicate setting {key}')
            values[key] = value
        for key, value in values.items():
            if not value and key in REPO_ENV.values() and key not in os.environ:
                os.environ[key] = ''
            if value and key not in os.environ:
                resolved = Path(os.path.expandvars(value)).expanduser()
                os.environ[key] = str((path.parent / resolved).resolve())

def select_targets(only, exclude):
    """Expand repository aliases and return selected runtime targets in service order."""
    selected = {key for name in only for key in TARGETS[name]} if only else set(SERVICES)
    selected -= {key for name in exclude or [] for key in TARGETS[name]}
    if not selected:
        raise ValueError('No application services selected')
    return [key for key in SERVICES if key in selected]
```

**Annotations:**

- `shlex.split(..., comments=True)` interprets quoting and comments without executing the file as shell code. `split('=', 1)` separates the setting name from its value.
- Only the four repository path settings and `FEATURE_ENV_STATE_DIR` are accepted. Duplicate or unknown keys fail early.
- Existing process environment variables win over values in `.env`.
- `$HOME`-style variables and `~` are expanded in values. Relative file-configured paths are resolved beside the `.env` file.
- An explicitly empty repository setting is preserved so `repository_paths()` can report it instead of quietly using a fallback.
- `select_targets()` expands aliases into image keys, removes exclusions, rejects an empty result, and returns the selection in the fixed `SERVICES` order.

**Source:** [src/feature-env.py](src/feature-env.py), lines 91–115.

```python
def repository_paths(state, targets, parent=None):
    """Resolve selected Git roots, including the local client whenever UI is selected."""
    base = Path(parent or state.get('repos', str(BUNDLE.parent / 'hrbc'))).expanduser().resolve()
    keys = set(targets)
    if 'ui' in keys:
        keys.add('client')
    paths = {}
    for key, name in REPOS.items():
        if key not in keys:
            continue
        variable = REPO_ENV[name]
        configured = os.environ.get(variable)
        if configured is not None and not configured.strip():
            raise ValueError(f'{variable} is empty; enter its repository root in your .env file')
        repo = Path(configured).expanduser().resolve() if configured is not None else base / name
        if not repo.is_dir():
            raise ValueError(f'{variable}: repository directory does not exist: {repo}')
        try:
            top = Path(output(['git', '-C', repo, 'rev-parse', '--show-toplevel'], stderr=subprocess.PIPE).strip()).resolve()
        except subprocess.CalledProcessError:
            raise ValueError(f'{variable}: not a Git working tree: {repo}') from None
        if top != repo.resolve():
            raise ValueError(f'{variable}: use the repository root {top}, not {repo}')
        paths[key] = repo
    return paths
```

- Selecting UI automatically adds `client`; there is no independently deployed client service.
- Repository-specific environment variables override the fallback parent directory.
- `git rev-parse --show-toplevel` confirms that each location is exactly a Git repository root. A path to a subfolder fails with the correct root in the error message.
- The returned dictionary maps build keys to `Path` objects, for example `ui → /.../ui` and `client → /.../api-client-privateapi`.

### 4.4 External commands, Docker inspection, and JSON state

**File path:** `src/feature-env.py`.

**Purpose:** Small helpers centralize command execution and state serialization.

**Source:** [src/feature-env.py](src/feature-env.py), lines 64–89.

```python
def run(args, **kw):
    """Run an argument list without shell interpolation and fail on nonzero exit."""
    return subprocess.run([str(a) for a in args], check=True, **kw)

def output(args, **kw):
    """Capture decoded stdout for commands whose results drive the workflow."""
    return run(args, stdout=subprocess.PIPE, **kw).stdout.decode()

def save(path, data):
    """Write private JSON state readable and writable only by its owner."""
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)

def escape_compose(value):
    """Preserve literal dollars when saved runtime values pass through Compose again."""
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [escape_compose(x) for x in value]
    if isinstance(value, dict):
        return {k: escape_compose(v) for k, v in value.items()}
    return value

def inspect(name):
    """Read the current Docker object's details instead of assuming runtime state."""
    return json.loads(output(['docker', 'inspect', name]))[0]
```

**Annotations:**

- `run()` passes an argument list to `subprocess.run` and enables `check=True`. A nonzero command exit becomes an exception. It does not implicitly interpolate shell expressions.
- `output()` captures stdout when another function needs to parse a result, such as `docker inspect` JSON.
- `save()` produces readable JSON and applies owner-only read/write permissions (`0600`). It writes directly to the destination; it is not a temporary-file-and-rename transaction.
- `escape_compose()` recursively doubles dollar signs. The baseline already contains resolved runtime values; escaping prevents Compose from treating literal dollars as another variable substitution when the JSON is reused.
- `inspect()` returns the first object from Docker's JSON array. Callers use its `Config`, `State`, `HostConfig`, and networking fields rather than guessing the current container settings.

### 4.5 Capturing the restore baseline: `prepare()`

**File path:** `src/feature-env.py`.

**Purpose:** Reconstruct the current Compose environment and preserve its application image references before deploying local builds.

**Source:** [src/feature-env.py](src/feature-env.py), lines 117–148.

```python
def prepare(args):
    """Capture the existing cluster's restore baseline without replacing containers."""
    if (STATE / 'state.json').exists():
        raise ValueError(f'{STATE} already exists; reuse it instead of overwriting restore state')
    args.project = args.source
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]+', args.source):
        raise ValueError('Invalid Compose project name')
    source = inspect(f'{args.source}-hrbcui-1')
    labels = source['Config']['Labels']
    files = labels['com.docker.compose.project.config_files'].split(',')
    # Our overrides replace the original Compose labels after deployment.
    # Re-preparing from them would lose the published-image restore baseline.
    baseline = next((Path(name) for name in files if Path(name).name == 'normal.json'), None)
    if baseline is not None:
        raise ValueError(
            f'Cluster already uses feature-env state from {baseline.parent}. '
            'Set FEATURE_ENV_STATE_DIR in your .env to that existing directory '
            '(containing state.json and normal.json); do not prepare a new restore baseline.'
        )
    STATE.mkdir(mode=0o700, exist_ok=True)
    # Copy only orchestration source; never SSH credentials or the whole shared volume.
    if not (STATE / 'compose').exists():
        if not args.settings_container:
            ids = output(['docker', 'ps', '-aq', '--filter', 'volume=devcontainer-settings']).split()
            if not ids:
                raise ValueError('No container mounts devcontainer-settings; pass --settings-container')
            args.settings_container = ids[0]
        run(['docker', 'cp', f'{args.settings_container}:/local/compose-hrbc', STATE / 'compose'])
    compose = STATE / 'compose'
    files = [compose / Path(f).name for f in files]
    if any(not p.is_file() for p in files):
        raise ValueError('Source uses extra Compose files; supply the matching checkout before continuing')
```

**Annotations:**

- Existing `state.json` is protected from overwrite.
- `args.project = args.source` selects the existing environment rather than creating a second cluster.
- Docker's Compose labels on the existing UI container reveal the Compose source file list.
- If those labels already mention `normal.json`, this environment is already using a saved baseline. The code tells the operator to reuse that state directory.
- The settings container is located by its `devcontainer-settings` volume if no explicit name was supplied.
- `docker cp` copies `/local/compose-hrbc` into the state folder. This obtains the orchestration definitions; it is not a copy of database contents.

**Source:** [src/feature-env.py](src/feature-env.py), lines 149–166.

```python
    defaults = compose / 'clusters/hrbc' / args.release / 'docker.env'
    raw = output(['bash', '-c', 'set -a; source "$1"; env -0', 'feature-env', defaults])
    env = os.environ.copy()
    env.update({k.removeprefix('PDOCKERV_'): v for x in raw.split('\0') if '=' in x for k, v in [x.split('=', 1)] if k.startswith('PDOCKERV_')})
    env['DNSGROUP'] = args.project
    for suffix, service in [('WEB', 'hrbcweblb'), ('OFFICE', 'hrbcoffice'), ('API', 'hrbcconnectapi')]:
        original = inspect(f'{args.source}-{service}-1')
        env['IP_' + suffix] = original['HostConfig']['PortBindings']['80/tcp'][0]['HostIp']
        host_service = 'hrbcproductweb' if suffix == 'WEB' else service
        host_env = dict(x.split('=', 1) for x in inspect(f'{args.source}-{host_service}-1')['Config']['Env'])
        env['URL_' + suffix] = host_env.get('DNSHOST_HRBC' if suffix == 'WEB' else 'DNSHOST_' + suffix, args.source + '-' + suffix.lower() + '.localvm')
    variables = set(re.findall(r'\$\{(\w+)\}', ''.join(p.read_text() for p in files)))
    if variables - env.keys():
        raise ValueError('Missing Compose variables: ' + ', '.join(sorted(variables - env.keys())))
    cmd = ['docker', 'compose', '-p', args.project]
    for p in files:
        cmd += ['-f', p]
    config = json.loads(output(cmd + ['config', '--format', 'json'], env=env))
```

- The release `docker.env` is explicitly sourced through Bash. This is a separate, trusted configuration input from the user's `.env`, which was parsed as data.
- `PDOCKERV_` prefixes are removed to supply the variable names expected by Compose.
- Existing IP bindings and hostnames are read from containers so the rendered configuration matches the local environment.
- Missing `${VARIABLE}` inputs fail before configuration is saved.
- `docker compose config --format json` resolves the Compose files into a JSON configuration. At this point it is inspecting/rendering configuration, not starting containers.

**Source:** [src/feature-env.py](src/feature-env.py), lines 167–192.

```python
    originals = {}
    for name, service in config['services'].items():
        # Match hrbc1's installed images, including tags changed since the release defaults.
        old = inspect(f'{args.source}-{name}-1')
        service['image'] = old['Config']['Image']
        runtime_env = dict(x.split('=', 1) for x in old['Config']['Env'])
        service['environment'] = runtime_env
        if name in SERVICES.values():
            originals[name] = service['image']
        if service.get('container_name') or service.get('network_mode'):
            raise ValueError(f'{name}: fixed container name or network mode requires review')
        for mount in service.get('volumes', []):
            if mount['type'] == 'bind':
                raise ValueError(f'{name}: bind mount requires explicit isolation review')
        for label in list(service.get('labels', {})):
            if label == 'porters.dynamic-group':
                service['labels'][label] = args.project
    for kind in ['networks', 'volumes']:
        for name, value in config.get(kind, {}).items():
            if value.get('external'):
                raise ValueError(f'External {kind}: {name}; refusing shared state')
            value['name'] = args.project + '_' + name
    config['name'] = args.project
    save(STATE / 'normal.json', escape_compose(config))
    save(STATE / 'state.json', {'project': args.project, 'source': args.source, 'repos': str(Path(args.repos or BUNDLE.parent / 'hrbc').expanduser().resolve()), 'originals': originals, 'daemon': output(['docker', 'info', '--format', '{{.ID}}']).strip()})
    print(f'Prepared image overrides for existing {args.source}; no containers changed.')
```

- Each service's image reference and environment are taken from its existing container, overriding release defaults.
- `originals` records image references for the four replaceable application services.
- Fixed container names, network modes, bind mounts, and external networks/volumes are rejected here; this implementation expects its supported Compose layout.
- Networks and volumes are named using the existing project prefix.
- `normal.json` stores the baseline Compose configuration. `state.json` stores project identity, fallback repository location, original application image references, and Docker daemon ID.
- These are image references from `Config.Image`, not exported image archives or a database backup. There is no `docker save` or database dump in this function.

### 4.6 Capturing local code: `snapshot()`

**File path:** `src/feature-env.py`.

**Purpose:** Build from a separate copy of the current saved working tree, including relevant uncommitted files.

**Source:** [src/feature-env.py](src/feature-env.py), lines 194–223.

```python
def snapshot(repo, destination):
    """Copy saved working-tree source while excluding credentials and generated output."""
    destination.mkdir(parents=True)
    # Current working files, including non-ignored untracked feature source. No .git/config or credentials.
    paths = output(['git', '-C', repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split('\0')
    for name in set(paths) - {''}:
        p = Path(name)
        if any(part in {'.git', '.devcontainer', '.vscode', 'node_modules', '.gradle'} for part in p.parts):
            continue
        static_source = p.parts[:2] in {
            ('static_source', directory) for directory in ('js', 'lib', 'themes', 'pages', 'extensions', 'common')
        }
        if any(part in {'build', 'dist'} for part in p.parts) and not static_source:
            continue
        if p.name in {'.npmrc', '.env', 'auth.pubkey.pem'} or p.suffix in {'.pem', '.key'} or p.name.startswith('.env.') or p.name.endswith('.local.env'):
            continue
        src = repo / p
        if not src.exists():
            continue
        if src.is_symlink():
            raise ValueError(f'Symlink in build source requires review: {src}')
        if src.is_file():
            target = destination / p
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    # The process umask restricts directories to 0700; runtime images need them traversable by their non-root user.
    for d in [destination, *destination.rglob('*')]:
        if d.is_dir():
            d.chmod(0o755)
    return {'repository': str(repo), 'branch': output(['git', '-C', repo, 'branch', '--show-current']).strip() or 'detached', 'sha': output(['git', '-C', repo, 'rev-parse', 'HEAD']).strip(), 'dirty': bool(output(['git', '-C', repo, 'status', '--porcelain']).strip())}
```

**Annotations:**

1. `git ls-files --cached --others --exclude-standard` obtains tracked files and non-ignored untracked files. `-z` separates filenames with NUL characters so spaces are handled correctly.
2. Files are copied from the current working directory contents, not fetched from the committed revision. Saved edits therefore participate in the build; unsaved editor buffers cannot.
3. Common tool directories, generated outputs, and named credential/configuration files are skipped. The static asset subtree exception preserves nested `build`/`dist` content needed by legacy static sources.
4. Missing files are skipped, reflecting local deletions. Existing symbolic links are rejected rather than followed.
5. `shutil.copy2()` preserves file metadata while copying into the build context. Directory permissions become `0755` so runtime users can traverse copied directories.
6. Returned metadata records the source repository, branch, HEAD commit, and whether Git reports changes. `dirty=True` explains why the snapshot can differ from the recorded commit.

The exclusions are a specific filename/path filter, not a general secret scanner. This function also does not lock your source repository: save your edits before building, as the README directs.

### 4.7 Building images: `build()` and nested `one()`

**File path:** `src/feature-env.py`.

**Purpose:** Prepare source snapshots, launch Docker builds, and record successful image selections.

**Source:** [src/feature-env.py](src/feature-env.py), lines 225–260.

```python
def build(args, state):
    """Build isolated snapshots concurrently; publish image selections only on success."""
    overall_started = time.monotonic()
    repos = repository_paths(state, args.targets, args.repos)
    print(f'Build targets: {", ".join(args.targets)} | parallel jobs: {args.jobs}', flush=True)
    print(json.dumps({'source_paths': {key: str(repo) for key, repo in repos.items()}}, indent=2), flush=True)
    if args.add_host:
        state['build_hosts'] = args.add_host
        save(STATE / 'state.json', state)
    else:
        args.add_host = state.get('build_hosts', [])
    build_id = uuid.uuid4().hex[:12]
    context = STATE / 'builds' / build_id
    context.mkdir(parents=True)
    metadata = {}
    source_keys = set(args.targets)
    if 'ui' in source_keys:
        source_keys.add('client')
    for position, (key, repo) in enumerate(repos.items(), 1):
        print(f'[{position}/{len(repos)}] Snapshot {key}: {repo}', flush=True)
        metadata[key] = snapshot(repo, context / key)
        meta = metadata[key]
        print(f'[{key}] Snapshot ready | {meta["branch"]} | {meta["sha"][:12]} | dirty={meta["dirty"]}', flush=True)
    if 'web' in source_keys:
        shutil.copy2(BUNDLE / 'web-static.conf', context / 'web-static.conf')
    # Version detection reads Git metadata, but never expose the source .git configuration.
    if 'api' in source_keys:
        run(['git', 'init', '-q', context / 'api'])
        run(['git', '-C', context / 'api', 'add', '.'])
        run(['git', '-C', context / 'api', '-c', 'user.name=Feature Build', '-c', 'user.email=feature@localhost', 'commit', '-qm', 'Integration source snapshot'])
        run(['git', '-C', context / 'api', 'tag', output(['git', '-C', repos['api'], 'describe', '--tags', '--abbrev=0']).strip()])
    images = {}
    for key in args.targets:
        sha = metadata[key]['sha'][:12]
        images[key] = f'local/{state["project"]}-{key}:feature-{sha}-{build_id}'
    failed = threading.Event()
```

**Annotations:**

- `time.monotonic()` measures elapsed time without depending on wall-clock corrections.
- Build-only hostname overrides are retained in state for later builds.
- `uuid.uuid4().hex[:12]` creates a build ID and a separate `builds/<id>/` folder.
- Each selected source is copied with `snapshot()`. UI includes `client`; web adds `web-static.conf` beside the source subfolders.
- For API builds, the copied `api/` folder gets a fresh Git repository and commit, then the source's nearest reachable tag. This supports application version detection without copying the original `.git` configuration. The commits occur inside the generated snapshot, not in your source repository.
- `images` maps each selected target to a unique local image tag. `failed` is a shared thread event used to cancel sibling builds on failure.

**Source:** [src/feature-env.py](src/feature-env.py), lines 261–303.

```python
    def one(key):
        if failed.is_set():
            raise ValueError('Cancelled after another build failed')
        logfile = context / (key + '.log')
        cmd = ['docker', 'build', '--progress=plain', '-f', BUNDLE / 'docker' / (key + '.Dockerfile'), '-t', images[key], '--build-arg', 'RUNTIME_IMAGE=' + state['originals'][SERVICES[key]]]
        for entry in args.add_host:
            cmd += ['--add-host', entry]
        if args.npmrc:
            cmd += ['--secret', 'id=npmrc,src=' + str(Path(args.npmrc).resolve())]
        cmd += [context]
        print(f'Building {key}: {images[key]} (log: {logfile})', flush=True)
        with logfile.open('w') as log:
            process = subprocess.Popen([str(x) for x in cmd], stdout=log, stderr=subprocess.STDOUT)
            started = time.monotonic()
            last_update = started
            with logfile.open() as progress:
                while True:
                    finished = process.poll() is not None
                    for line in progress.readlines():
                        stage = re.match(r'(#\d+) \[([^\]]+)\]', line)
                        result = re.match(r'(#\d+) (DONE|CACHED|ERROR)(?: |$)', line)
                        if stage:
                            print(f'[{key}] {stage[1]} {stage[2]} | {int(time.monotonic() - started)}s elapsed', flush=True)
                        elif result:
                            print(f'[{key}] {result[1]} {result[2]}', flush=True)
                    if finished:
                        break
                    if failed.is_set() or time.monotonic() - started > 1800:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise ValueError(f'{key}: build cancelled or timed out')
                    if time.monotonic() - last_update >= 15:
                        print(f'[{key}] Still building | {int(time.monotonic() - started)}s elapsed | log: {logfile}', flush=True)
                        last_update = time.monotonic()
                    time.sleep(0.5)
            if process.returncode:
                failed.set()
                raise ValueError(f'{key}: Docker build failed')
        print(f'Built {key} in {int(time.monotonic() - started)}s', flush=True)
```

- `one(key)` is a nested worker function. It shares `context`, `images`, `state`, and `failed` from `build()`.
- The `docker build` invocation connects the selected Dockerfile, original runtime image, optional secret, and source snapshot.
- `Popen` starts the process without blocking immediately. Both stdout and stderr go to `<target>.log`.
- The polling loop reads that log and prints selected BuildKit progress lines. `DONE` means a completed step, `CACHED` means Docker reused a prior result, and `ERROR` identifies a reported failure.
- Every fifteen seconds without the periodic update, it emits a still-building message. The loop sleeps half a second to avoid continuously consuming CPU.
- A cancellation event or thirty-minute limit terminates the Docker CLI process, then kills that process if it does not exit within ten seconds.
- A nonzero return code marks the build failed and propagates an exception.

**Source:** [src/feature-env.py](src/feature-env.py), lines 304–322.

```python
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(one, key): key for key in args.targets}
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except BaseException:
                failed.set()
                for pending in futures:
                    pending.cancel()
                raise ValueError(f'Build failed for {futures[f]}: {f.exception()}. Saved deployment images unchanged. Full log: {context / (futures[f] + ".log")}')
    previous = json.loads((STATE / 'build.json').read_text()) if (STATE / 'build.json').exists() else {}
    all_images = {**previous.get('images', {}), **images}
    all_metadata = {**previous.get('repositories', {}), **metadata}
    save(STATE / 'feature.json', {'services': {SERVICES[key]: {'image': image, 'pull_policy': 'never'} for key, image in all_images.items()}})
    save(STATE / 'build.json', {'repositories': all_metadata, 'images': all_images, 'build_id': build_id})
    print(json.dumps({'repositories': metadata, 'images': images}, indent=2))
    elapsed = int(time.monotonic() - overall_started)
    print(f'Build completed in {elapsed}s. Images are built, not deployed.', flush=True)
    print('Next: bash', shlex.quote(str(BUNDLE / 'run' / 'deploy.sh')), '--only', *args.targets, flush=True)
```

- `ThreadPoolExecutor(max_workers=args.jobs)` limits concurrent image builds. Each worker runs its own Docker CLI process.
- `f.result()` surfaces worker exceptions. On failure, pending workers are canceled and running workers observe the shared event.
- Only after all selected builds succeed does the code merge their image/provenance records into the previous records. Previously built unselected targets remain available.
- `feature.json` contains Compose image overrides; `build.json` contains image tags, source metadata, and the latest build ID.
- A failed Docker build leaves the previous deployment-selection files unchanged, although successful intermediate/local images and logs may remain. The two final JSON writes are sequential, so this is not a crash-atomic transaction across both files.

### 4.8 Deploying containers and managing supporting services

**File path:** `src/feature-env.py`.

**Purpose:** `deploy()` selects the built images, checks network aliases, starts support services, and asks Compose to run the chosen application services.

**Source:** [src/feature-env.py](src/feature-env.py), lines 420–437.

```python
def deploy(state, targets):
    """Replace only selected apps, check health, then refresh shared load-balancer routing."""
    feature = json.loads((STATE / 'feature.json').read_text())
    services = [SERVICES[key] for key in targets]
    if set(feature['services']) - set(SERVICES.values()):
        raise ValueError('Feature override contains unexpected services')
    if set(services) - set(feature['services']):
        raise ValueError('Build selected services before deploying: ' + ', '.join(targets))
    for service in services:
        run(['docker', 'image', 'inspect', feature['services'][service]['image']], stdout=subprocess.DEVNULL)
    check_redirects(state, targets)
    start_dependencies(state)
    run(compose(state) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '300', '--pull', 'never', *services])
    start_dependencies(state, after_apps=True)
    if 'web' in targets:
        refresh_web_routing(state)
    run(['docker', 'restart', state['project'] + '-hrbcprivateapicore-1', state['project'] + '-hrbcweblb-1'])
    summary(state, targets)
```

**Annotations:**

- `feature.json` must contain only allowed application services and include every requested target.
- `docker image inspect` verifies that each chosen image exists locally before deployment.
- `check_redirects()` detects development containers that still hold the application's network name.
- Supporting containers are started before and after the selected application update as required by their dependency relationships.
- `up -d` runs services in the background. `--no-deps` prevents Compose from also recreating dependencies; this program handles existing support containers separately.
- `--wait --wait-timeout 300` waits for the selected services to reach the required running/healthy state. `--pull never` requires the locally available images.
- Web deployment may update the React asset routing rule.
- Both shared routing containers, `hrbcprivateapicore` and `hrbcweblb`, are restarted after successful deployment even when a single application was selected.
- The function finishes by printing actual container status. There is no automatic call to `restore()` if deployment or a health check fails.

**Source:** [src/feature-env.py](src/feature-env.py), lines 348–364.

```python
def check_redirects(state, targets):
    """Refuse deployment when another container holds a selected application's alias."""
    config = json.loads((STATE / 'normal.json').read_text())
    aliases = {SERVICES[key] for key in targets}
    if 'api' in targets:
        aliases.add('hrbcprivateapicore')
    for definition in config.get('networks', {}).values():
        network = definition['name']
        details = json.loads(output(['docker', 'network', 'inspect', network]))[0]
        for container_id in details.get('Containers', {}):
            container = inspect(container_id)
            if container['Config'].get('Labels', {}).get('com.docker.compose.project') == state['project']:
                continue
            attached = container['NetworkSettings']['Networks'].get(network, {})
            conflicts = aliases.intersection(attached.get('Aliases') or [])
            if conflicts:
                raise ValueError(f"{container['Name']} still redirects {', '.join(sorted(conflicts))}; use its existing network-disconnect task before deploying")
```

`check_redirects()` reads the saved networks, inspects their attached containers, and looks for conflicting aliases held by containers outside the saved Compose project. For API selection it also checks `hrbcprivateapicore`. A network alias is a name other containers use to find a service; a development container still holding that name could redirect requests away from the new application.

**Source:** [src/feature-env.py](src/feature-env.py), lines 367–397.

```python
def dependency_order(services):
    """Visit dependencies before dependents, rejecting cyclic Compose definitions."""
    ordered, visiting, visited = [], set(), set()
    def visit(name):
        if name in visiting:
            raise ValueError('Cyclic Compose dependency: ' + name)
        if name in visited:
            return
        visiting.add(name)
        for dependency in services[name].get('depends_on', {}):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)
    for name in services:
        visit(name)
    return ordered

def start_dependencies(state, after_apps=False):
    """Start existing dependencies before or after apps without recreating their containers."""
    config = json.loads((STATE / 'normal.json').read_text())
    blocked = set(SERVICES.values())
    order = dependency_order(config['services'])
    for name in order:
        if any(dep in blocked for dep in config['services'][name].get('depends_on', {})):
            blocked.add(name)
        if name in SERVICES.values() or (name in blocked) != after_apps:
            continue
        container = state['project'] + '-' + name + '-1'
        if not inspect(container)['State']['Running']:
            run(['docker', 'start', container])
```

`dependency_order()` performs a depth-first traversal of Compose `depends_on` relationships. `visiting` detects cycles, `visited` avoids duplicate work, and appending after visiting dependencies produces dependency-first order.

`start_dependencies()` starts existing non-application containers using `docker start` only if they are stopped. `blocked` initially contains application services; it expands to services depending on those applications. The first pass starts support services independent of applications, and the `after_apps=True` pass starts the remaining support services. It considers the saved cluster's support services, not only the selected application's dependency subtree, and does not itself wait for their health checks.

**Source:** [src/feature-env.py](src/feature-env.py), lines 400–417.

```python
def refresh_web_routing(state):
    """Align the standard React route with deployed PHP's asset version when needed."""
    version = output(['docker', 'exec', state['project'] + '-hrbcproductweb-1',
                      'cat', '/var/www/hrbc/systeminfo/version.txt']).partition('\n')[0].strip()
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', version):
        raise ValueError('Unexpected deployed PHP asset version: ' + version)
    container = inspect(state['project'] + '-hrbcweblb-1')
    environment = dict(entry.split('=', 1) for entry in container['Config']['Env'])
    current = environment.get('FRONTEND_REACT', '')
    desired = 'path_beg /P-' + version + '/tsbundle'
    if current == desired:
        return
    if not re.fullmatch(r'path_beg /P-[0-9]+(?:\.[0-9]+)+/tsbundle', current):
        raise ValueError('Unexpected web React routing rule; review FRONTEND_REACT before deployment')
    save(STATE / 'routing.json', {'services': {'hrbcweblb': {'environment': {'FRONTEND_REACT': desired}}}})
    print(f'Aligning web React routing with deployed PHP: {current} -> {desired}', flush=True)
    run(compose(state, False) + ['-f', STATE / 'routing.json', 'up', '-d', '--no-deps',
                                '--wait', '--wait-timeout', '180', '--pull', 'never', 'hrbcweblb'])
```

`refresh_web_routing()` reads the version from the deployed PHP container, validates that it is a dotted numeric version, and constructs `path_beg /P-<version>/tsbundle`. If the load balancer already uses that rule, it returns. Otherwise it validates the old rule's expected format, writes `routing.json`, and updates only `hrbcweblb` using the baseline plus that routing override. This keeps the route aligned with URLs emitted by the deployed PHP code.

### 4.9 Status, stop, and restore

**File path:** `src/feature-env.py`.

**Purpose:** Report the actual running image, stop selected services, or return them to the saved baseline.

**Source:** [src/feature-env.py](src/feature-env.py), lines 331–345.

```python
def summary(state, targets=None):
    """Show actual container health and provenance only when its image matches the build."""
    provenance = json.loads((STATE / 'build.json').read_text()) if (STATE / 'build.json').exists() else {}
    for key in targets if targets is not None else SERVICES:
        service = SERVICES[key]
        container = inspect(state['project'] + '-' + service + '-1')
        current = container['Config']['Image']
        status = container['State'].get('Health', {}).get('Status', container['State']['Status'])
        meta = provenance.get('repositories', {}).get(key, {}) if current == provenance.get('images', {}).get(key) else {}
        print(f"{service}: {status} | {current}")
        if meta:
            print(f"  {meta['repository']} | {meta['branch']} | {meta['sha']} | dirty={meta['dirty']}")
        if key == 'ui' and meta:
            client = provenance['repositories']['client']
            print(f"  bundled client: {client['repository']} | {client['branch']} | {client['sha']} | dirty={client['dirty']}")
```

`summary()` inspects each selected container instead of assuming the latest build is deployed. It displays Docker health status when present, otherwise the container's ordinary state. Source provenance is printed only when the current image reference matches the recorded build; UI status also prints its bundled client metadata.

**Source:** [src/feature-env.py](src/feature-env.py), lines 503–513.

```python
    elif args.command == 'restore':
        # Reuse the baseline without local image overrides; database contents are untouched.
        check_redirects(state, args.targets)
        run(compose(state, False) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *[SERVICES[key] for key in args.targets]])
        if 'web' in args.targets:
            refresh_web_routing(state)
        run(['docker', 'restart', state['project'] + '-hrbcprivateapicore-1', state['project'] + '-hrbcweblb-1'])
    elif args.command == 'stop':
        run(compose(state, False) + ['stop', *[SERVICES[key] for key in args.targets]])
    else:
        summary(state, args.targets)
```

- `restore` uses `compose(state, False)`, so local image overrides are absent. Compose runs the selected application services with the saved baseline configuration.
- Restore repeats the network alias check, waits up to 180 seconds, refreshes web routing when applicable, and restarts the shared routing containers.
- This restores application image/configuration choices, not local Git changes or database rows. This branch contains no database restore or volume deletion operation.
- `stop` stops selected application services without removing their containers or volumes.
- Restore does not include deploy's explicit `--pull never`; its image availability behavior follows the baseline/Compose settings. Keeping `state.json` and `normal.json` preserves the image references, not an immutable backup of the image bytes.

### 4.10 UI image and local client integration

**File path:** `src/docker/ui.Dockerfile`.

**Purpose:** Build the local API client and frontend together, then replace the frontend bundle in the original nginx runtime image.

**Source:** [src/docker/ui.Dockerfile](src/docker/ui.Dockerfile), lines 1–30.

```dockerfile
# syntax=docker/dockerfile:1
# Compile the local client and UI snapshots, then serve the bundle in the saved runtime.
# COPY paths refer to the generated snapshot context, not this Dockerfile's directory.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/node:AZ2023-N16.20.2 AS build
ENV npm_config_fetch_retries=3 npm_config_fetch_timeout=300000 npm_config_maxsockets=5
ENV npm_config_registry=http://registry.ps.porters.local:8081/repository/npm-group/
# Cache downloads while mounting npm credentials only for dependency installation.
WORKDIR /src/client
COPY client/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY client/ ./
RUN npm run build && npm pack --ignore-scripts && mv *.tgz /tmp/client.tgz
WORKDIR /src/ui
COPY ui/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY ui/ ./
# Keep the UI lockfile dependency tree intact when substituting the local library.
# Fail explicitly if the local library introduces dependencies the UI does not supply.
RUN node -e 'const semver=require("semver"); const p=require("/src/client/package.json"); for(const [name,range] of Object.entries({...p.dependencies,...p.peerDependencies})){const v=require(name+"/package.json").version;if(!semver.satisfies(v,range))throw Error(name+" does not satisfy local client dependency "+range);}' \
    && rm -rf node_modules/@hrbc/api-client-private \
    && mkdir -p node_modules/@hrbc/api-client-private \
    && tar -xzf /tmp/client.tgz --strip-components=1 -C node_modules/@hrbc/api-client-private \
    && npm run build
# Keep the existing nginx startup configuration, replacing only the compiled bundle.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /home/nginx/www/build
COPY --from=build /src/ui/build/ /home/nginx/www/build/
# Readiness checks asset delivery, not authenticated application behavior.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD curl -fsS -H 'Host: feature.localvm' http://127.0.0.1/tsbundle/asset-manifest.json >/dev/null || exit 1
```

**Block-by-block annotations:**

1. **Build stage:** `FROM ...N16.20.2 AS build` starts a named stage containing Node tooling. `ARG RUNTIME_IMAGE` declares the argument used by the later `FROM`.
2. **Dependency download configuration:** the `ENV` instructions choose the private npm registry and retry/timeout settings.
3. **Client dependencies:** `WORKDIR /src/client` chooses the container build directory. Copying `package*.json` before all source lets Docker reuse the installation layer when only application source changes.
4. **BuildKit mounts:** `type=secret` makes supplied npm configuration available to that `RUN` step without a normal `COPY` into the image. `type=cache` retains npm's download cache across builds. `npm ci --ignore-scripts` installs from the lockfile without package lifecycle scripts.
5. **Client package:** `npm run build` compiles the client; `npm pack` creates a package archive; it is renamed to `/tmp/client.tgz` for the UI step.
6. **UI dependency tree:** the UI first installs its own lockfile dependencies. The `node -e` command reads the local client's dependencies and peer dependencies and checks the versions available from the UI with `semver.satisfies()`.
7. **Local library substitution:** the existing `@hrbc/api-client-private` folder is removed. The freshly built archive is extracted into that location, with its outer directory stripped. `npm run build` then compiles the UI with the local client.
8. **Runtime stage:** `FROM ${RUNTIME_IMAGE}` starts again from the saved application image. The old UI bundle is removed, and only the new compiled `build/` is copied from the build stage. These instructions do not introduce a new `CMD` or `ENTRYPOINT`; startup comes from the runtime base image.
9. **Readiness:** `curl -fsS` requests the asset manifest from nginx inside the container. `127.0.0.1` refers to that container, and the `Host` header selects the expected virtual host. Failure returns exit status `1`.

This is a multi-stage build: compiler workspace and final application runtime are distinct stages. It is not live source mounting; changes made after the snapshot require another build and deployment.

### 4.11 Proxy image

**File path:** `src/docker/proxy.Dockerfile`.

**Purpose:** Compile the proxy and replace its runtime code and production dependencies.

**Source:** [src/docker/proxy.Dockerfile](src/docker/proxy.Dockerfile), lines 1–20.

```dockerfile
# syntax=docker/dockerfile:1
# Compile the proxy snapshot with Node tooling kept outside the final runtime stage.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/node:AZ2023-N22.22.0 AS build
ENV npm_config_fetch_retries=3 npm_config_fetch_timeout=300000 npm_config_maxsockets=5
ENV npm_config_registry=http://registry.ps.porters.local:8081/repository/npm-group/
WORKDIR /src
# Install from the lockfile; credentials are temporary and downloads are cached.
COPY proxy/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY proxy/ ./
RUN npm run build && npm prune --omit=dev --ignore-scripts
# Preserve runtime startup behavior but replace old code and production dependencies.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /home/node/app/dist /home/node/app/node_modules
COPY --from=build --chown=node:node /src/dist/ /home/node/app/dist/
COPY --from=build --chown=node:node /src/node_modules/ /home/node/app/node_modules/
COPY --from=build --chown=node:node /src/package.json /home/node/app/package.json
# The proxy status endpoint signals readiness with HTTP 204.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD node -e "require('http').get('http://127.0.0.1:3000/privateapi/status',r=>process.exit(r.statusCode===204?0:1)).on('error',()=>process.exit(1))"
```

**Block-by-block annotations:**

- The build stage uses Node 22.22.0 and the same temporary npm secret/download-cache pattern as UI.
- `COPY proxy/package*.json ./` followed by `npm ci` separates dependency installation from source copying.
- `npm run build` generates the application output. `npm prune --omit=dev --ignore-scripts` removes development-only packages before runtime copying.
- The final stage starts from the saved proxy image, removes the previous `dist` and `node_modules`, and copies in the rebuilt replacements plus `package.json`.
- `--chown=node:node` sets ownership for the runtime application user.
- The health check uses Node's HTTP library to request `/privateapi/status` on port 3000. Only HTTP 204 passes; a different status or connection error exits with failure.

### 4.12 Java API image

**File path:** `src/docker/api.Dockerfile`.

**Purpose:** Compile the local shared Java modules and PrivateAPI WAR, then install that WAR into the saved Tomcat runtime.

**Source:** [src/docker/api.Dockerfile](src/docker/api.Dockerfile), lines 1–27.

```dockerfile
# syntax=docker/dockerfile:1
# Build the Java PrivateAPI WAR from the HRBC snapshot and locally built shared modules.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/tomcat:AZ2023-J11.0.29-T7.0.76 AS build
# Select an installed JDK explicitly so Gradle has compiler tools, not just a JRE.
RUN dnf install -y git java-11-amazon-corretto-devel && dnf clean all
RUN set -eu; javac_path=$(find /usr/lib/jvm -type f -name javac | head -n 1); test -n "$javac_path"; ln -s "$(dirname "$(dirname "$javac_path")")" /opt/feature-java
ENV GRADLE_OPTS=-Dfile.encoding=UTF-8
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8
ENV JAVA_HOME=/opt/feature-java
ENV PATH=/opt/feature-java/bin:${PATH}

COPY api/ /src/
WORKDIR /src/api_source
# Publish shared modules in dependency order before assembling the consuming WAR.
RUN --mount=type=cache,target=/root/.gradle \
    set -eu; for project in UtilGeneral Core CoreLegacy HrbcDb HrbcGeneral; do \
      (cd "$project" && bash ./gradlew --no-daemon --console=plain assemble publishMainPublicationToMavenLocal); \
    done; \
    cd PrivateAPI; bash ./gradlew --no-daemon --console=plain assemble; \
    mkdir /out; cp build/libs/*.war /out/PrivateAPI.war
# Remove the old exploded app so Tomcat deploys the replacement WAR on startup.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /usr/share/tomcat7/webapps/PrivateAPI
COPY --from=build /out/PrivateAPI.war /usr/share/tomcat7/webapps/PrivateAPI.war
# Check the application's no-auth Memcached status endpoint after startup.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=24 CMD curl -fsS http://127.0.0.1:8080/PrivateAPI/privateapi/noauth/status/memcache >/dev/null || exit 1
```

**Block-by-block annotations:**

- `dnf install` adds Git and a Java 11 development kit. A JDK contains `javac`, the compiler; a runtime alone is insufficient for compiling source.
- The `find` command locates `javac`; two `dirname` calls locate its JDK root; `/opt/feature-java` becomes a stable symlink. `JAVA_HOME` and `PATH` then select it for Gradle.
- UTF-8 JVM/Gradle settings make source/build text handling explicit.
- `COPY api/ /src/` copies the generated HRBC snapshot; the working directory becomes its `api_source` folder.
- The loop builds and publishes `UtilGeneral`, `Core`, `CoreLegacy`, `HrbcDb`, and `HrbcGeneral` in the written order. Publishing to Maven Local makes their artifacts available for the subsequent consumer build in this build stage.
- Each `(cd ... && ...)` runs in a subshell, so moving into one module does not change the directory for the next loop iteration.
- `PrivateAPI` is assembled afterward. Its WAR archive is copied to `/out/PrivateAPI.war` as a predictable transfer path.
- In the runtime stage, the old expanded `PrivateAPI` application directory is removed and the new WAR replaces the old archive. Tomcat's existing startup behavior deploys the archive.
- The health check requests the application's unauthenticated Memcached status endpoint. The longer start period and retry count allow more Java startup time. A successful check demonstrates that endpoint responds; it does not test every authenticated feature.

### 4.13 Legacy web image

**File path:** `src/docker/web.Dockerfile`.

**Purpose:** Build legacy JavaScript/CSS assets, copy local PHP source, and overlay the compiled assets into the saved web runtime.

**Source:** [src/docker/web.Dockerfile](src/docker/web.Dockerfile), lines 1–39.

```dockerfile
# syntax=docker/dockerfile:1
# Compile legacy assets, then package local PHP and static source into the saved runtime.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/tomcat:AZ2023-J11.0.29-T7.0.76 AS build
RUN dnf install -y ant cpio java-11-amazon-corretto-devel && dnf clean all
RUN set -eu; javac_path=$(find /usr/lib/jvm -path '*java-11*' -type f -name javac | head -n 1); test -n "$javac_path"; ln -s "$(dirname "$(dirname "$javac_path")")" /opt/feature-java
ENV GRADLE_OPTS=-Dfile.encoding=UTF-8
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8
ENV JAVA_HOME=/opt/feature-java
ENV PATH=/opt/feature-java/bin:${PATH}

COPY web/static_source/ /src/static_source/
WORKDIR /src/static_source
# Ant can finish despite missing assets; require the main JS and both stylesheets.
RUN ant -f build.xml \
	&& test -s built-results/js/jquery.js \
	&& test -s built-results/themes/porters.css \
	&& test -s built-results/themes/portersLogin.css
FROM ${RUNTIME_IMAGE}
# A clean replacement prevents deleted local files surviving from the base image.
RUN rm -rf /var/www/static /var/www/hrbc
COPY web/product/ /var/www/hrbc/
# Validate the production bootstrap and grant the runtime user its writable directories.
RUN test -s /var/www/hrbc/yii-1.1.29.f89b76/framework/yiilite.php \
	&& test -s /var/www/hrbc/htdocs/site/index.php.production \
	&& mkdir -p /var/www/hrbc/htdocs/site/assets /var/www/hrbc/files /var/www/hrbc/runtime-view \
	&& chown -R www-data:www-data /var/www/hrbc/htdocs/site/assets /var/www/hrbc/files /var/www/hrbc/runtime-view
# Include all dev-served static trees, then overlay their production build outputs.
COPY web/static_source/js/ /var/www/static/js/
COPY web/static_source/lib/ /var/www/static/lib/
COPY web/static_source/themes/ /var/www/static/themes/
COPY web/static_source/pages/ /var/www/static/pages/
COPY web/static_source/extensions/ /var/www/static/extensions/
COPY web/static_source/common/ /var/www/static/common/
COPY --from=build /src/static_source/built-results/ /var/www/static/
# Python stages this Apache config in the snapshot context; .htaccess is not enabled.
COPY web-static.conf /etc/httpd/conf.d/feature-static.conf
# Check both JS and CSS using the version emitted by the deployed PHP source.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD version=$(head -n 1 /var/www/hrbc/systeminfo/version.txt) && curl -fsS "http://127.0.0.1/P-${version}/js/jquery.js" >/dev/null && curl -fsS "http://127.0.0.1/P-${version}/themes/porters.css" >/dev/null || exit 1
```

**Block-by-block annotations:**

1. The build stage installs Ant, `cpio`, and a JDK, then selects Java 11 explicitly.
2. Ant runs the copied `static_source/build.xml`. The three `test -s` checks require nonempty `jquery.js`, `porters.css`, and `portersLogin.css`, because the original comment notes that Ant can finish despite missing assets.
3. The final stage removes the old PHP/static trees before copying replacements. This prevents a source file deleted locally from surviving as a stale file in the base image's application directory.
4. `COPY web/product/ /var/www/hrbc/` installs the saved local PHP source. The two bootstrap-file checks verify expected Yii and production entry files exist.
5. `mkdir` and `chown` prepare directories writable by `www-data` for application assets/files/runtime views.
6. All listed static source trees are copied, then `built-results/` is overlaid. Generated assets therefore take precedence where paths overlap.
7. The Apache configuration staged by Python is copied into the server's configuration directory.
8. The health check reads the first version-file line and requests versioned JavaScript and CSS URLs. Both requests must succeed.

### 4.14 Apache asset configuration

**File path:** `src/web-static.conf`.

**Purpose:** Configure caching behavior for the local legacy asset URLs.

**Source:** [src/web-static.conf](src/web-static.conf), lines 1–13.

```apache
# Loaded as server configuration because the runtime does not allow .htaccess overrides.
# Enable response-header directives only if the module is not already present.
<IfModule !headers_module>
    LoadModule headers_module modules/mod_headers.so
</IfModule>

# Keep versioned local assets uncached across rebuilds and disable directory listings.
<LocationMatch "^/P-[^/]+/(js|lib|themes|pages|extensions|common)(/|$)">
    Options -Indexes
    ExpiresActive Off
    Header set Cache-Control "no-store"
    Header unset ETag
</LocationMatch>
```

- `<IfModule !headers_module>` loads `mod_headers` only when it is not already loaded.
- `LocationMatch` targets versioned paths beginning `/P-.../` for the enumerated legacy asset categories.
- `Options -Indexes` disables directory listings for those locations.
- `ExpiresActive Off`, `Cache-Control: no-store`, and removing `ETag` discourage reuse of stale assets across local rebuilds.
- These rules are server configuration because the runtime does not allow the intended `.htaccess` overrides. They do not match every application route; for example, `tsbundle` is not one of the listed categories.

### 4.15 Operator documentation and ignored local files

**File paths:** `src/README.md`, `src/.gitignore`.

**Purpose:** Provide configuration/command instructions and keep generated local files out of ordinary Git tracking.

**Source:** [src/.gitignore](src/.gitignore), lines 1–8.

```gitignore
# Configuration and captured cluster state are private to each developer's machine.
.env
.env.local
.feature-env/
.feature-env.lock
# Generated Python caches are not source files.
__pycache__/
*.zip
```

The default `.env`, state folder, and lock are ignored. Python caches and ZIP archives are also ignored. These patterns do not automatically cover every custom `FEATURE_ENV_STATE_DIR` or custom environment-file path; the README tells operators to keep their configuration, logs, state, and snapshots private.

The operator-facing build sequence is taken directly from the README:

**Source:** [src/README.md](src/README.md), lines 36–45.

````markdown
## Build And Deploy

Save changes and disconnect Dev Container aliases for selected services first.
The first build automatically captures restore state. Build does not deploy.

```bash
bash run/build.sh
bash run/deploy.sh
```
````

This separates build and deploy and asks users to disconnect Dev Container aliases first. The underlying implementation of those two instructions is `snapshot()`/`build()` and `check_redirects()`/`deploy()`, respectively.

## 5. Key Workflows in Code

### 5.1 Build UI with an edited local API client

**Operator command, run from `src/`:**

```bash
bash run/build.sh --only ui
```

**Step 1 — Bash selects the Python command.**

**Source:** [src/run/build.sh](src/run/build.sh), lines 4–4.

```bash
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" build "$@"
```

**Source:** [src/run/feature-env.sh](src/run/feature-env.sh), lines 5–5.

```bash
exec python3 "$(dirname "$(readlink -f "$0")")/../feature-env.py" "$@"
```

The first script passes `build --only ui`; the second replaces itself with Python. `main()` parses the arguments, loads `.env`, obtains the lock, and prepares the existing cluster on first use.

**Step 2 — Selecting UI also selects the client source.**

**Source:** [src/feature-env.py](src/feature-env.py), lines 93–96.

```python
    base = Path(parent or state.get('repos', str(BUNDLE.parent / 'hrbc'))).expanduser().resolve()
    keys = set(targets)
    if 'ui' in keys:
        keys.add('client')
```

**Source:** [src/feature-env.py](src/feature-env.py), lines 243–245.

```python
    for position, (key, repo) in enumerate(repos.items(), 1):
        print(f'[{position}/{len(repos)}] Snapshot {key}: {repo}', flush=True)
        metadata[key] = snapshot(repo, context / key)
```

`repository_paths()` returns both repository roots. `build()` passes each into `snapshot()`, which returns source metadata while copying saved files into the generated context.

**Step 3 — Python invokes Docker with the UI recipe.**

**Source:** [src/feature-env.py](src/feature-env.py), lines 265–270.

```python
        cmd = ['docker', 'build', '--progress=plain', '-f', BUNDLE / 'docker' / (key + '.Dockerfile'), '-t', images[key], '--build-arg', 'RUNTIME_IMAGE=' + state['originals'][SERVICES[key]]]
        for entry in args.add_host:
            cmd += ['--add-host', entry]
        if args.npmrc:
            cmd += ['--secret', 'id=npmrc,src=' + str(Path(args.npmrc).resolve())]
        cmd += [context]
```

For `key == 'ui'`, `-f` points to `src/docker/ui.Dockerfile`, and `RUNTIME_IMAGE` is the saved original `hrbcui` image reference. Docker reads the generated context's `client/` and `ui/` folders.

**Step 4 — The recipe packages the client and substitutes it into UI.**

**Source:** [src/docker/ui.Dockerfile](src/docker/ui.Dockerfile), lines 12–13.

```dockerfile
COPY client/ ./
RUN npm run build && npm pack --ignore-scripts && mv *.tgz /tmp/client.tgz
```

**Source:** [src/docker/ui.Dockerfile](src/docker/ui.Dockerfile), lines 20–24.

```dockerfile
RUN node -e 'const semver=require("semver"); const p=require("/src/client/package.json"); for(const [name,range] of Object.entries({...p.dependencies,...p.peerDependencies})){const v=require(name+"/package.json").version;if(!semver.satisfies(v,range))throw Error(name+" does not satisfy local client dependency "+range);}' \
    && rm -rf node_modules/@hrbc/api-client-private \
    && mkdir -p node_modules/@hrbc/api-client-private \
    && tar -xzf /tmp/client.tgz --strip-components=1 -C node_modules/@hrbc/api-client-private \
    && npm run build
```

The new client archive replaces the installed client package only after dependency compatibility checks pass. UI then compiles against that replacement.

**Step 5 — Docker creates the runtime image, and Python records its name.**

**Source:** [src/docker/ui.Dockerfile](src/docker/ui.Dockerfile), lines 26–28.

```dockerfile
FROM ${RUNTIME_IMAGE}
RUN rm -rf /home/nginx/www/build
COPY --from=build /src/ui/build/ /home/nginx/www/build/
```

**Source:** [src/feature-env.py](src/feature-env.py), lines 314–318.

```python
    previous = json.loads((STATE / 'build.json').read_text()) if (STATE / 'build.json').exists() else {}
    all_images = {**previous.get('images', {}), **images}
    all_metadata = {**previous.get('repositories', {}), **metadata}
    save(STATE / 'feature.json', {'services': {SERVICES[key]: {'image': image, 'pull_policy': 'never'} for key, image in all_images.items()}})
    save(STATE / 'build.json', {'repositories': all_metadata, 'images': all_images, 'build_id': build_id})
```

Docker returns an exit status to the worker; success lets the thread complete. After all selected workers succeed, `build()` writes the Compose image override and provenance. No container is replaced in this workflow.

### 5.2 Deploy the previously built UI

**Operator command, run from `src/`:**

```bash
bash run/deploy.sh --only ui
```

**Step 1 — The launcher maps deploy to `up`.**

**Source:** [src/run/deploy.sh](src/run/deploy.sh), lines 4–4.

```bash
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" up "$@"
```

**Source:** [src/feature-env.py](src/feature-env.py), lines 501–502.

```python
    elif args.command == 'up':
        deploy(state, args.targets)
```

`main()` calls `deploy(state, ['ui'])`; `SERVICES` maps that selection to `hrbcui`.

**Step 2 — Verify the image and network aliases, then invoke Compose.**

**Source:** [src/feature-env.py](src/feature-env.py), lines 428–436.

```python
    for service in services:
        run(['docker', 'image', 'inspect', feature['services'][service]['image']], stdout=subprocess.DEVNULL)
    check_redirects(state, targets)
    start_dependencies(state)
    run(compose(state) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '300', '--pull', 'never', *services])
    start_dependencies(state, after_apps=True)
    if 'web' in targets:
        refresh_web_routing(state)
    run(['docker', 'restart', state['project'] + '-hrbcprivateapicore-1', state['project'] + '-hrbcweblb-1'])
```

`compose(state)` supplies both `normal.json` and `feature.json`. The selected UI gets the new image with its saved environment/network configuration. Existing support containers may be started, and the two shared routing containers are restarted after success. The web-specific routing refresh is skipped because `web` was not selected.

**Step 3 — Docker evaluates the image's health check while Compose waits.**

**Source:** [src/docker/ui.Dockerfile](src/docker/ui.Dockerfile), lines 29–30.

```dockerfile
# Readiness checks asset delivery, not authenticated application behavior.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD curl -fsS -H 'Host: feature.localvm' http://127.0.0.1/tsbundle/asset-manifest.json >/dev/null || exit 1
```

The check runs inside the UI container and tests delivery of `asset-manifest.json`. Docker reports health, and Compose's `--wait` uses that status. This is an asset-readiness check, not a browser login test.

**Step 4 — Print status from the actual container.**

**Source:** [src/feature-env.py](src/feature-env.py), lines 336–345.

```python
        container = inspect(state['project'] + '-' + service + '-1')
        current = container['Config']['Image']
        status = container['State'].get('Health', {}).get('Status', container['State']['Status'])
        meta = provenance.get('repositories', {}).get(key, {}) if current == provenance.get('images', {}).get(key) else {}
        print(f"{service}: {status} | {current}")
        if meta:
            print(f"  {meta['repository']} | {meta['branch']} | {meta['sha']} | dirty={meta['dirty']}")
        if key == 'ui' and meta:
            client = provenance['repositories']['client']
            print(f"  bundled client: {client['repository']} | {client['branch']} | {client['sha']} | dirty={client['dirty']}")
```

When the image matches `build.json`, output includes the UI source branch/commit and the client bundled into that UI. If deployment fails, the top-level handler reports an error; rollback is a separate explicit command.

### 5.3 Restore the saved original application

**Operator command, run from `src/`:**

```bash
bash run/restore.sh --only ui
```

**Step 1 — Forward to the restore branch.**

**Source:** [src/run/restore.sh](src/run/restore.sh), lines 4–4.

```bash
exec bash "$(dirname "$(readlink -f "$0")")/feature-env.sh" restore "$@"
```

**Source:** [src/feature-env.py](src/feature-env.py), lines 503–509.

```python
    elif args.command == 'restore':
        # Reuse the baseline without local image overrides; database contents are untouched.
        check_redirects(state, args.targets)
        run(compose(state, False) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *[SERVICES[key] for key in args.targets]])
        if 'web' in args.targets:
            refresh_web_routing(state)
        run(['docker', 'restart', state['project'] + '-hrbcprivateapicore-1', state['project'] + '-hrbcweblb-1'])
```

The selected service is resolved exactly as during deployment. Network alias conflicts still block the change.

**Step 2 — Exclude the local-image override.**

**Source:** [src/feature-env.py](src/feature-env.py), lines 324–329.

```python
def compose(state, feature=True):
    """Use the saved baseline, optionally overlaid with successfully built local images."""
    cmd = ['docker', 'compose', '-p', state['project'], '-f', STATE / 'normal.json']
    if feature:
        cmd += ['-f', STATE / 'feature.json']
    return cmd
```

Because restore passes `False`, `feature.json` is not appended. Compose receives the baseline `normal.json` only, so its saved original image reference becomes the desired UI image.

**Step 3 — Compose applies the baseline and routing containers restart.**

The restore branch's `up` command waits for the selected service, then restarts the shared routing containers. A web restore additionally recalculates the route from the restored PHP version. Local source files, built images, and saved build metadata remain available; no database rollback is performed.

### Reading outcomes correctly

| Observation | Code that explains it |
| --- | --- |
| “I built successfully, but the browser still shows the old app.” | `build()` only saves image choices; `deploy()` is a separate command. |
| “My edited client code appears in the UI build.” | `repository_paths()` adds `client`, and `ui.Dockerfile` substitutes its package before compiling UI. |
| “A file edit after building did not appear.” | `snapshot()` copied the files before Docker built the image; there is no live source mount in these recipes. |
| “Only UI was selected, but routing containers restarted.” | `deploy()` and the restore branch explicitly restart both shared routing containers. |
| “A failed build did not change the next deployment image.” | `feature.json` is updated after all selected Docker builds succeed. |
| “Restoring the image did not restore my database.” | The restore branch runs Compose with saved application configuration; it performs no database restore. |
| “Status shows an image but no branch metadata.” | `summary()` prints provenance only when the actual image matches the recorded local build. |

All of these behaviors follow from the excerpts above. The repository provides build/deployment orchestration and readiness probes; it contains no application test suite or automatic database migration/rollback workflow of its own.

