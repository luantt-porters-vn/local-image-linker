#!/usr/bin/env python3
"""Build local source images and deploy or restore selected existing Compose services.

Launchers live in run/ and image recipes in docker/. Configuration and private
restore/build state remain relative to this file, independent of the caller's cwd.
"""
import argparse
import concurrent.futures
import fcntl
import threading
import time
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import shlex
import tempfile

BUNDLE = Path(__file__).resolve().parent
STATE = BUNDLE / '.feature-env'
SERVICES = {'ui': 'hrbcui', 'proxy': 'hrbcprivateapiproxy', 'api': 'hrbcprivateapicoreapp', 'web': 'hrbcproductweb'}
# Each key gets its own snapshot/context/image, even 'api' and 'web' which both build from the 'hrbc' repo.
REPOS = {'ui': 'ui', 'proxy': 'openapi-proxy', 'api': 'hrbc', 'web': 'hrbc', 'client': 'api-client-privateapi'}
REPO_ENV = {'ui': 'FEATURE_ENV_UI_REPO', 'openapi-proxy': 'FEATURE_ENV_PROXY_REPO',
            'hrbc': 'FEATURE_ENV_HRBC_REPO', 'api-client-privateapi': 'FEATURE_ENV_CLIENT_REPO'}
TARGETS = {key: (key,) for key in SERVICES}
TARGETS.update({'openapi-proxy': ('proxy',), 'hrbc': ('api', 'web'), 'api-client-privateapi': ('ui',)})

def load_environment(path):
    """Parse allowed path settings as data; exported environment values take precedence."""
    allowed = set(REPO_ENV.values()) | {'FEATURE_ENV_STATE_DIR', 'FEATURE_ENV_RELEASE'}
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
            elif key == 'FEATURE_ENV_RELEASE':
                # Not a filesystem path; keep the raw release string as-is.
                if value and key not in os.environ:
                    os.environ[key] = value
            elif value and key not in os.environ:
                resolved = Path(os.path.expandvars(value)).expanduser()
                os.environ[key] = str((path.parent / resolved).resolve())

def select_targets(only, exclude):
    """Expand repository aliases and return selected runtime targets in service order."""
    selected = {key for name in only for key in TARGETS[name]} if only else set(SERVICES)
    selected -= {key for name in exclude or [] for key in TARGETS[name]}
    if not selected:
        raise ValueError('No application services selected')
    return [key for key in SERVICES if key in selected]

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

def heal_stray_renames(state):
    """Recover a container Compose left under its temporary swap name after an interrupted recreate."""
    for service in SERVICES.values():
        expected = state['project'] + '-' + service + '-1'
        try:
            inspect(expected)
            continue
        except subprocess.CalledProcessError:
            pass
        ids = output(['docker', 'ps', '-aq',
                      '--filter', f'label=com.docker.compose.project={state["project"]}',
                      '--filter', f'label=com.docker.compose.service={service}']).split()
        if len(ids) != 1:
            continue  # nothing to repair, or ambiguous; let the normal flow surface the error
        current = inspect(ids[0])['Name'].lstrip('/')
        if current != expected:
            print(f'Repairing interrupted container swap: {current} -> {expected}', flush=True)
            run(['docker', 'rename', current, expected])

def container_source(value):
    """Parse an explicit container checkout without guessing a container or volume."""
    match = re.fullmatch(r'docker://([a-zA-Z0-9][a-zA-Z0-9_.-]*)(/[^\x00\r\n]*)', value)
    if not match or '..' in Path(match[2]).parts or match[2] == '/':
        raise ValueError('Container source must be docker://<container-name-or-id>/<absolute-repo-path> (no ..)')
    return match[1], match[2].rstrip('/')


def volume_source(value):
    """Paths are relative to the volume root, not the Dev Container workspace."""
    match = re.fullmatch(r'volume://([a-zA-Z0-9][a-zA-Z0-9_.-]+)(/[^\x00\r\n]*)?', value)
    if not match or '..' in Path(match[2] or '/').parts:
        raise ValueError('Volume source must be volume://<volume-name>[/repo-subdirectory] (no ..)')
    return match[1], (match[2] or '').rstrip('/')


def validate_repository(repo, label):
    if not repo.is_dir():
        raise ValueError(f'{label}: repository directory does not exist: {repo}')
    try:
        top = Path(output(['git', '-C', repo, 'rev-parse', '--show-toplevel'], stderr=subprocess.PIPE).strip()).resolve()
    except subprocess.CalledProcessError:
        raise ValueError(f'{label}: not a Git working tree: {repo}') from None
    if top != repo.resolve():
        raise ValueError(f'{label}: use the repository root {top}, not {repo}')


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
        if configured is not None and configured.startswith(('docker://', 'volume://')):
            if key not in {'ui', 'proxy'}:
                raise ValueError(f'{variable}: container and volume sources are supported only for ui and openapi-proxy')
            (volume_source if configured.startswith('volume://') else container_source)(configured)
            paths[key] = configured
            continue
        repo = Path(configured).expanduser().resolve() if configured is not None else base / name
        validate_repository(repo, variable)
        paths[key] = repo
    return paths

def image_release(image):
    """Read the HRBC release from published runtime tags, never from local source."""
    match = re.search(r'-H([0-9]+(?:\.[0-9]+)+)$', image)
    return match[1].replace('.', '-') if match else None


def cluster_release(project, requested=None):
    """Office is not replaced by local builds, so it remains the cluster release anchor."""
    image = inspect(f'{project}-hrbcoffice-1')['Config']['Image']
    detected = image_release(image)
    if not detected:
        raise ValueError(f'Cannot detect the cluster release from {image}; use a published HRBC office image.')
    if requested and requested != 'auto' and requested != detected:
        raise ValueError(f'Configured release {requested} does not match running cluster {detected}. '
                         'Update FEATURE_ENV_RELEASE / the extension version setting, or leave it blank for automatic detection.')
    return detected


def validate_original_image(service, image, release):
    if image.startswith('local/'):
        raise ValueError(f'{service} uses a local image; start the published cluster before capturing its restore baseline.')
    if service in (SERVICES['web'], SERVICES['api']) and image_release(image) != release:
        raise ValueError(f'{service} image {image} does not match cluster {release}; finish the cluster upgrade before Build.')


def baseline_release(state):
    return state.get('release') or image_release(state['originals'].get(SERVICES['web'], ''))


def ensure_current_baseline(args, state):
    """Capture an upgraded published cluster without losing the old restore baseline."""
    global STATE
    previous = baseline_release(state)
    if previous == args.release:
        return state
    if args.command != 'build':
        raise ValueError(f'Saved baseline release {previous or "unknown"} differs from cluster {args.release}. '
                         'Run Build to refresh the baseline before deploying or restoring.')
    for service in SERVICES.values():
        image = inspect(f'{state["project"]}-{service}-1')['Config']['Image']
        if image.startswith('local/'):
            raise ValueError(f'Cluster upgraded to {args.release}, but {service} still runs {image}. '
                             'Start the upgraded cluster with its published application images, then run Build again. '
                             'The existing restore baseline has been preserved.')
    original_state_dir = STATE
    args.source = state['source']
    # Prepare in isolation: a failed capture leaves every active state file intact.
    with tempfile.TemporaryDirectory(prefix='feature-env-refresh-', dir=STATE.parent) as temporary:
        try:
            STATE = Path(temporary)
            prepare(args)
        finally:
            STATE = original_state_dir
        replacement = Path(temporary)
        backup = STATE / ('backup-release-' + uuid.uuid4().hex[:12])
        backup.mkdir(mode=0o700)
        names = ('state.json', 'normal.json', 'build.json', 'feature.json', 'routing.json')
        for name in names:
            if (STATE / name).exists():
                shutil.copy2(STATE / name, backup / name)
        for name in ('state.json', 'normal.json'):
            (replacement / name).replace(STATE / name)
        for name in ('build.json', 'feature.json', 'routing.json'):
            (STATE / name).unlink(missing_ok=True)
        print(f'Cluster release changed: {previous} -> {args.release}. Previous state: {backup}', flush=True)
    return json.loads((STATE / 'state.json').read_text())


def check_build_release(targets, release):
    """A partial rebuild must not make old images eligible for deployment."""
    path = STATE / 'build.json'
    builds = json.loads(path.read_text()) if path.exists() else {}
    releases = builds.get('releases', {})
    stale = [key for key in targets if releases.get(key) != release]
    if stale:
        raise ValueError(f'Rebuild {", ".join(stale)} for cluster {release} before deploying; '
                         'their saved builds have an old or unverified release.')


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
    defaults = compose / 'clusters/hrbc' / args.release / 'docker.env'
    if not defaults.is_file():
        raise ValueError(f'Missing release definition {defaults}; update devcontainer-settings for cluster {args.release}.')
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
    originals = {}
    for name, service in config['services'].items():
        # Match hrbc1's installed images, including tags changed since the release defaults.
        old = inspect(f'{args.source}-{name}-1')
        service['image'] = old['Config']['Image']
        runtime_env = dict(x.split('=', 1) for x in old['Config']['Env'])
        service['environment'] = runtime_env
        if name in SERVICES.values():
            validate_original_image(name, service['image'], args.release)
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
    save(STATE / 'state.json', {'project': args.project, 'source': args.source, 'repos': str(Path(args.repos or BUNDLE.parent / 'hrbc').expanduser().resolve()), 'originals': originals, 'release': args.release, 'daemon': output(['docker', 'info', '--format', '{{.ID}}']).strip()})
    print(f'Prepared image overrides for existing {args.source}; no containers changed.')

def snapshot(repo, destination, volume_image=None):
    """Copy saved working-tree source while excluding credentials and generated output."""
    if isinstance(repo, str) and repo.startswith('volume://'):
        volume, subdirectory = volume_source(repo)
        try:
            run(['docker', 'volume', 'inspect', volume], stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            raise ValueError(f'Volume {volume} does not exist on the current Docker daemon; '
                             'check docker volume ls. No volume was created.') from None
        if not volume_image:
            raise ValueError('A local runtime image is required for the volume snapshot helper')
        # Reuse the selected cluster runtime image, without pulling or executing it.
        # nocopy prevents Docker populating an empty source volume from that image.
        helper = output(['docker', 'create', '--pull', 'never', '--network', 'none', '--read-only',
                         '--mount', f'type=volume,source={volume},target=/image-linker-source,readonly,volume-nocopy',
                         '--entrypoint', '/bin/true', volume_image]).strip()
        try:
            metadata = snapshot(f'docker://{helper}/image-linker-source{subdirectory}', destination)
            metadata['repository'] = repo
            return metadata
        finally:
            # -v removes only anonymous helper volumes, never the named source volume.
            run(['docker', 'rm', '-v', helper], stdout=subprocess.DEVNULL)
    if isinstance(repo, str) and repo.startswith('docker://'):
        container, source = container_source(repo)
        # docker cp also reads stopped containers. Keep the complete checkout outside
        # the Docker build context and remove it even when validation/copying fails.
        with tempfile.TemporaryDirectory(prefix='image-linker-source-') as temporary:
            checkout = Path(temporary) / 'repo'
            checkout.mkdir()
            try:
                run(['docker', 'cp', f'{container}:{source}/.', checkout])
            except subprocess.CalledProcessError:
                raise ValueError(f'Cannot copy {repo}; check the container name and repository path. '
                                 'The container must still exist, but may be stopped.') from None
            if not (checkout / '.git').is_dir() or (checkout / '.git').is_symlink():
                raise ValueError(f'{repo}: expected a standalone Git checkout with a .git directory; '
                                 'linked worktrees are not supported for container sources')
            validate_repository(checkout, repo)
            metadata = snapshot(checkout, destination)
            metadata['repository'] = repo
            return metadata
    destination.mkdir(parents=True)
    # Current working files, including non-ignored untracked feature source. No .git/config or credentials.
    paths = output(['git', '-C', repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split('\0')
    for name in set(paths) - {''}:
        p = Path(name)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError(f'Unsafe build source path: {name}')
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
        if any((repo / Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)):
            raise ValueError(f'Symlink in build source requires review: {src}')
        if not src.exists():
            continue
        if src.is_file():
            target = destination / p
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    # The process umask restricts directories to 0700; runtime images need them traversable by their non-root user.
    for d in [destination, *destination.rglob('*')]:
        if d.is_dir():
            d.chmod(0o755)
    return {'repository': str(repo), 'branch': output(['git', '-C', repo, 'branch', '--show-current']).strip() or 'detached', 'sha': output(['git', '-C', repo, 'rev-parse', 'HEAD']).strip(), 'dirty': bool(output(['git', '-C', repo, 'status', '--porcelain']).strip())}

class BuildProgress:
    """Render concurrent builds in fixed rows, or sparse messages outside a terminal."""

    def __init__(self, targets, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.tty = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.rows = {key: {'status': 'Queued', 'step': '', 'started': None, 'ended': None}
                     for key in targets}
        self.lock = threading.Lock()
        self.lines = 0
        self.last_report = time.monotonic()

    def update(self, key, status=None, step=None):
        with self.lock:
            row = self.rows[key]
            if step is not None:
                # Docker output must not inject terminal controls into the display.
                row['step'] = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', step)
            if status is not None:
                row['status'] = status
                if status == 'Building':
                    row['started'] = time.monotonic()
                elif status in ('Built', 'Failed', 'Cancelled'):
                    row['ended'] = time.monotonic()
                if not self.tty:
                    print(self.format_row(key, row), file=self.stream, flush=True)

    def format_row(self, key, row):
        elapsed = '' if row['started'] is None else f"{int((row['ended'] or time.monotonic()) - row['started'])}s"
        step = row['step'] if row['status'] == 'Building' else ''
        return f"  {key:<7} {row['status']:<10} {elapsed:>6}  {step}".rstrip()

    def render(self, final=False):
        with self.lock:
            if self.tty:
                width = max(1, shutil.get_terminal_size().columns - 1)
                done = sum(row['status'] == 'Built' for row in self.rows.values())
                lines = [f'Building images [{done}/{len(self.rows)}]']
                lines += [self.format_row(key, row) for key, row in self.rows.items()]
                if self.lines:
                    self.stream.write(f'\x1b[{self.lines}A')
                for line in lines:
                    self.stream.write('\r\x1b[2K' + line[:width] + '\n')
                self.stream.flush()
                self.lines = len(lines)
            elif not final and time.monotonic() - self.last_report >= 15:
                for key, row in self.rows.items():
                    if row['status'] == 'Building':
                        print(self.format_row(key, row), file=self.stream, flush=True)
                self.last_report = time.monotonic()


def prepare_api_history(checkout, tag):
    """Give Gradle's version detection a tagged, clean history of api_source only.

    Fixed identity and dates plus a stat-free index make .git byte-identical whenever api_source
    is, so edits elsewhere in the HRBC repo (PHP, static assets) keep the Docker build cache.
    """
    identity = {'GIT_AUTHOR_NAME': 'Feature Build', 'GIT_AUTHOR_EMAIL': 'feature@localhost',
                'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z', 'GIT_COMMITTER_NAME': 'Feature Build',
                'GIT_COMMITTER_EMAIL': 'feature@localhost', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}
    env = {**os.environ, **identity}
    run(['git', 'init', '-q', '-b', 'main', checkout])
    # Automatic gc would pack objects into files named nondeterministically; keep them loose.
    run(['git', '-C', checkout, 'config', 'gc.auto', '0'])
    run(['git', '-C', checkout, 'config', 'maintenance.auto', 'false'])
    run(['git', '-C', checkout, 'add', 'api_source'])
    run(['git', '-C', checkout, 'commit', '-q', '--no-verify', '-m', 'Integration source snapshot'], env=env)
    run(['git', '-C', checkout, 'tag', tag])
    # The index records inode/ctime of this particular copy; rebuild it from HEAD without stat data.
    (checkout / '.git' / 'index').unlink()
    run(['git', '-C', checkout, 'read-tree', 'HEAD'])


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
        metadata[key] = snapshot(repo, context / key, volume_image=state['originals'][SERVICES[key]]
                                 if key in {'ui', 'proxy'} else None)
        meta = metadata[key]
        print(f'[{key}] Snapshot ready | {meta["branch"]} | {meta["sha"][:12]} | dirty={meta["dirty"]}', flush=True)
    if 'web' in source_keys:
        shutil.copy2(BUNDLE / 'web-static.conf', context / 'web-static.conf')
    # Version detection reads Git metadata, but never expose the source .git configuration.
    if 'api' in source_keys:
        prepare_api_history(context / 'api', output(['git', '-C', repos['api'], 'describe', '--tags', '--abbrev=0']).strip())
    images = {}
    for key in args.targets:
        sha = metadata[key]['sha'][:12]
        images[key] = f'local/{state["project"]}-{key}:feature-{sha}-{build_id}'
    failed = threading.Event()
    display = BuildProgress(args.targets)
    print(f'Full build logs: {context}/<target>.log', flush=True)
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
        display.update(key, status='Building', step='Starting Docker')
        with logfile.open('w') as log:
            process = subprocess.Popen([str(x) for x in cmd], stdout=log, stderr=subprocess.STDOUT)
            started = time.monotonic()
            with logfile.open() as progress:
                while True:
                    finished = process.poll() is not None
                    for line in progress.readlines():
                        stage = re.match(r'(#\d+) (\[[^\]]+\] .+|exporting .+)', line)
                        if stage:
                            display.update(key, step=f'{stage[1]} {stage[2].strip()}')
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
                    time.sleep(0.5)
            if process.returncode:
                raise ValueError(f'{key}: Docker build failed')
        display.update(key, status='Built')
    def tracked(key):
        try:
            one(key)
        except BaseException:
            display.update(key, status='Cancelled' if failed.is_set() else 'Failed')
            failed.set()
            raise

    display.render()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(tracked, key): key for key in args.targets}
            pending = set(futures)
            errors = []
            while pending:
                completed, pending = concurrent.futures.wait(
                    pending, timeout=0.2, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in completed:
                    if future.cancelled():
                        display.update(futures[future], status='Cancelled')
                    elif future.exception() is not None:
                        errors.append((futures[future], future.exception()))
                        failed.set()
                        for waiting in pending:
                            waiting.cancel()
                display.render()
            if errors:
                key, error = errors[0]
                raise ValueError(f'Build failed for {key}: {error}. Saved deployment images unchanged. Full log: {context / (key + ".log")}')
    finally:
        display.render(final=True)
    previous = json.loads((STATE / 'build.json').read_text()) if (STATE / 'build.json').exists() else {}
    all_images = {**previous.get('images', {}), **images}
    all_metadata = {**previous.get('repositories', {}), **metadata}
    save(STATE / 'feature.json', {'services': {SERVICES[key]: {'image': image, 'pull_policy': 'never'} for key, image in all_images.items()}})
    save(STATE / 'build.json', {'repositories': all_metadata, 'images': all_images, 'build_id': build_id,
                              'releases': {**previous.get('releases', {}), **{key: args.release for key in args.targets}}})
    print(json.dumps({'repositories': metadata, 'images': images}, indent=2))
    elapsed = int(time.monotonic() - overall_started)
    print(f'Build completed in {elapsed}s. Images are built, not deployed.', flush=True)
    print('Next: bash', shlex.quote(str(BUNDLE / 'run' / 'deploy.sh')), '--only', *args.targets, flush=True)

def compose(state, feature=True):
    """Use the saved baseline, optionally overlaid with successfully built local images."""
    cmd = ['docker', 'compose', '-p', state['project'], '-f', STATE / 'normal.json']
    if feature:
        cmd += ['-f', STATE / 'feature.json']
    return cmd

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


def restart_routing(state, targets):
    """Refresh only routers whose application backends may have changed address."""
    routers = []
    if 'api' in targets:
        routers.append('hrbcprivateapicore')
    if set(targets).intersection({'ui', 'proxy', 'web'}):
        routers.append('hrbcweblb')
    if routers:
        run(['docker', 'restart', *[state['project'] + '-' + router + '-1' for router in routers]])


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


def clean(state, targets, keep_builds, dry_run):
    """Remove local feature images and stale build snapshots no longer referenced by saved state."""
    build_info = json.loads((STATE / 'build.json').read_text()) if (STATE / 'build.json').exists() else {'images': {}, 'build_id': None}
    feature_info = json.loads((STATE / 'feature.json').read_text()) if (STATE / 'feature.json').exists() else {'services': {}}
    keep_images = set()
    for key in targets:
        service = SERVICES[key]
        if service in feature_info['services']:
            keep_images.add(feature_info['services'][service]['image'])
        if key in build_info['images']:
            keep_images.add(build_info['images'][key])
        try:
            keep_images.add(inspect(f"{state['project']}-{service}-1")['Config']['Image'])
        except subprocess.CalledProcessError:
            pass
    existing = output(['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}',
                        '--filter', f'reference=local/{state["project"]}-*']).split()
    removable = sorted(set(existing) - keep_images)
    verb = 'Would remove' if dry_run else 'Removing'
    if removable:
        print(f'{verb} unused local images:')
        for image in removable:
            print(' ', image)
        if not dry_run:
            subprocess.run(['docker', 'rmi'] + removable, check=False)
    else:
        print('No unused local images to remove.')
    builds_dir = STATE / 'builds'
    all_builds = sorted((p for p in builds_dir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime) if builds_dir.exists() else []
    boundary = len(all_builds) - keep_builds if keep_builds > 0 else 0
    stale_builds = [p for p in all_builds[:max(boundary, 0)] if p.name != build_info.get('build_id')]
    if stale_builds:
        print(f'{verb} stale build snapshots:')
        for path in stale_builds:
            print(' ', path)
            if not dry_run:
                shutil.rmtree(path)
    else:
        print('No stale build snapshots to remove.')
    if not dry_run:
        subprocess.run(['docker', 'image', 'prune', '-f'], check=False)


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
    restart_routing(state, targets)
    summary(state, targets)


def main():
    """Parse commands, load private configuration, lock state and enforce daemon identity."""
    global STATE
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'paths', 'build', 'up', 'restore', 'stop', 'status', 'clean'])
    parser.add_argument('--env-file', type=Path, help='Configuration file; defaults to .env beside this script')
    parser.add_argument('--source', default='hrbc1')
    parser.add_argument('--release', default=None, help='Expected cluster release; defaults to FEATURE_ENV_RELEASE, then automatic detection')
    parser.add_argument('--settings-container', help='Existing stopped container mounting devcontainer-settings')
    parser.add_argument('--repos', help='Fallback parent of the four repositories; FEATURE_ENV_*_REPO variables override individual paths')
    parser.add_argument('--add-host', action='append', default=[], help='Optional build-only hostname:IP mapping for VPN/Docker DNS')
    parser.add_argument('--npmrc', help='Private npm config mounted as a BuildKit secret')
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=2)
    parser.add_argument('--dry-run', action='store_true', help='clean: list what would be removed without removing it')
    parser.add_argument('--keep-builds', type=int, default=2, help='clean: most recent build snapshots to retain per target selection')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--only', nargs='+', choices=TARGETS, help='Build/manage only these repositories or runtime targets')
    selection.add_argument('--exclude', nargs='+', choices=TARGETS, help='Leave these repositories or runtime targets to Dev Containers')
    args = parser.parse_args()
    env_file = (args.env_file or BUNDLE / '.env').expanduser().resolve()
    if not env_file.is_file():
        raise ValueError(f'Environment file not found: {env_file}. Create and edit it using the README configuration before running this command.')
    load_environment(env_file)
    STATE = Path(os.environ.get('FEATURE_ENV_STATE_DIR', str(BUNDLE / '.feature-env'))).expanduser().resolve()
    if args.release is None:
        args.release = os.environ.get('FEATURE_ENV_RELEASE')
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
    if args.command in {'prepare', 'build', 'up', 'restore'}:
        state_path = STATE / 'state.json'
        project = json.loads(state_path.read_text())['project'] if state_path.exists() else args.source
        args.release = cluster_release(project, args.release)
        print(f'Cluster release: {args.release}', flush=True)
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
    heal_stray_renames(state)
    if args.command in {'build', 'up', 'restore'}:
        state = ensure_current_baseline(args, state)
    if args.command == 'build':
        build(args, state)
    elif args.command == 'up':
        check_build_release(args.targets, args.release)
        deploy(state, args.targets)
    elif args.command == 'restore':
        # Reuse the baseline without local image overrides; database contents are untouched.
        check_redirects(state, args.targets)
        run(compose(state, False) + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *[SERVICES[key] for key in args.targets]])
        if 'web' in args.targets:
            refresh_web_routing(state)
        restart_routing(state, args.targets)
    elif args.command == 'stop':
        run(compose(state, False) + ['stop', *[SERVICES[key] for key in args.targets]])
    elif args.command == 'clean':
        clean(state, args.targets, args.keep_builds, args.dry_run)
    else:
        summary(state, args.targets)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'feature-env: {exc}', file=sys.stderr)
        sys.exit(1)
