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
import zipfile

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

def package_bundle(destination):
    """Export only public tooling and blank configuration, never local state or secrets."""
    files = ['feature-env.py', 'run/feature-env.sh', '.gitignore', 'README.md',
             'run/build.sh', 'run/deploy.sh', 'run/restore.sh',
             'docker/ui.Dockerfile', 'docker/proxy.Dockerfile', 'docker/api.Dockerfile',
             'docker/web.Dockerfile', 'web-static.conf']
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(BUNDLE / name, 'feature-env/' + name)
        config = '# Enter your local Git repository roots before building. This file is never executed.\n'
        config += ''.join(f'{variable}=""\n' for variable in REPO_ENV.values())
        config += 'FEATURE_ENV_STATE_DIR=".feature-env"\n'
        archive.writestr('feature-env/.env', config)
    print(f'Package created: {destination}\nIncludes a blank .env for the recipient to edit. Your local configuration, state, logs and source snapshots were excluded.')

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


def main():
    """Parse commands, load private configuration, lock state and enforce daemon identity."""
    global STATE
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'paths', 'build', 'up', 'restore', 'stop', 'status', 'package'])
    parser.add_argument('--env-file', type=Path, help='Configuration file; defaults to .env beside this script')
    parser.add_argument('--output', type=Path, help='New ZIP filename for package; existing files are never overwritten')
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
    if args.command == 'package':
        package_bundle((args.output or BUNDLE.parent / 'feature-env.zip').expanduser().resolve())
        return
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
    if args.command == 'prepare':
        prepare(args)
        return
    if args.command == 'build' and not (STATE / 'state.json').exists():
        repository_paths({}, args.targets, args.repos)
        if (STATE / 'normal.json').exists():
            raise ValueError('Incomplete preparation: normal.json exists without state.json; review before retrying')
        print(f'First build: preparing existing cluster {args.source} (release {args.release}). No containers will be replaced.', flush=True)
        prepare(args)
    state = json.loads((STATE / 'state.json').read_text())
    if state.get('daemon') and output(['docker', 'info', '--format', '{{.ID}}']).strip() != state['daemon']:
        raise ValueError('Docker daemon changed since preparation')
    if state['project'] != state['source']:
        raise ValueError('State targets a separate project; prepare existing-cluster state first')
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

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'feature-env: {exc}', file=sys.stderr)
        sys.exit(1)
