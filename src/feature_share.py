"""Temporary, certificate-pinned LAN transport for Docker image bundles."""
import fcntl
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit
from terminal_ui import ui


PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
TARGETS = {'ui', 'proxy', 'api', 'web'}
CHUNK = 1024 * 1024


def private_address(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError('Sharing requires a private IPv4 LAN address, not a public or loopback address')
    return address


def lan_interfaces():
    interfaces = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        for _, name in socket.if_nameindex():
            if name.startswith(('lo', 'docker', 'veth', 'br-', 'virbr', 'tun', 'tap', 'wg', 'tailscale')):
                continue
            request = struct.pack('256s', name.encode()[:15])
            try:
                address = socket.inet_ntoa(fcntl.ioctl(connection, 0x8915, request)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(connection, 0x891b, request)[20:24])
                private_address(address)
                interfaces.append((name, address, ipaddress.ip_network(f'{address}/{mask}', strict=False)))
            except (OSError, ValueError):
                continue
    return interfaces


def sharing_address(requested=None):
    interfaces = lan_interfaces()
    if requested:
        interfaces = [entry for entry in interfaces if entry[1] == requested]
    if not interfaces:
        raise ValueError('No usable LAN interface. Run sharing on the LAN-connected host, or check WSL/container networking. '
                         '--share-address must be a private IPv4 address assigned to this environment.')
    if len(interfaces) > 1:
        choices = ', '.join(f'{name}={address}' for name, address, _ in interfaces)
        if not os.isatty(0):
            raise ValueError(f'Multiple LAN interfaces: {choices}. Select one with --share-address.')
        ui.section('Select LAN Interface')
        ui.detail('Available', choices)
        chosen = ui.prompt('Enter the LAN IPv4 address coworkers can reach')
        return sharing_address(chosen)
    name, address, network = interfaces[0]
    ui.section('LAN Preflight')
    ui.detail('Interface', name)
    ui.detail('Address', address)
    ui.detail('Peer subnet', network)
    release = Path('/proc/sys/kernel/osrelease').read_text().lower()
    if 'microsoft' in release:
        ui.info('WSL detected. NAT sharing will offer managed Windows LAN access with an administrator prompt.')
    elif Path('/.dockerenv').exists():
        ui.warning('Running inside WSL or a container. This address may not be reachable from Wi-Fi.')
        ui.info('Use host networking/mirrored WSL where supported, or expose the port through the host.')
        ui.info('Forwarding/NAT may hide the peer address. Enforce LAN-only access in the host firewall too.')

    ui.info('Allow incoming access only on the private LAN. Do not enable public port forwarding.')
    return address, network


def check_listener(requested, port):
    address, network = sharing_address(requested)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.bind((address, port))
    except OSError as error:
        raise ValueError(f'Cannot listen on {address}:{port}. Stop an existing share or choose another --share-port; '
                         'the address must belong to this environment.') from error
    return address, network


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            checksum.update(block)
    return checksum.hexdigest()


def certificate(directory, run):
    arguments = ['req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '1',
                 '-subj', '/CN=Local Image Linker', '-keyout', str(directory / 'key.pem'),
                 '-out', str(directory / 'cert.pem')]
    if shutil.which('openssl'):
        run(['openssl', *arguments], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        ui.info('OpenSSL is unavailable on the host; Docker will obtain the alpine/openssl helper image. '
            'Only temporary certificate files are mounted; the helper runs without network access.')
        arguments[-3] = '/cert/key.pem'
        arguments[-1] = '/cert/cert.pem'
        run(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
             '--mount', f'type=bind,src={directory},dst=/cert',
             'alpine/openssl:latest', *arguments], stdout=subprocess.DEVNULL)
    encoded = ssl.PEM_cert_to_DER_cert((directory / 'cert.pem').read_text())
    return hashlib.sha256(encoded).hexdigest()


def export_bundle(directory, builds, targets, release, run, output):
    images = {}
    repositories = {}
    for key in targets:
        if builds.get('releases', {}).get(key) != release or key not in builds.get('images', {}):
            raise ValueError(f'Build {key} for release {release} before sharing')
        image = builds['images'][key]
        details = json.loads(output(['docker', 'image', 'inspect', image]))[0]
        images[key] = {'id': details['Id'], 'os': details['Os'], 'architecture': details['Architecture'],
                       'variant': details.get('Variant', ''), 'tag': image}
        for source in ([key, 'client'] if key == 'ui' else [key]):
            meta = builds.get('repositories', {}).get(source)
            if meta:
                repositories[source] = {field: meta[field] for field in ('repository', 'branch', 'sha', 'dirty')}
    archive = directory / 'images.tar'
    estimate = sum(json.loads(output(['docker', 'image', 'inspect', entry['id']]))[0]['Size'] for entry in images.values())
    if shutil.disk_usage(directory).free < estimate + CHUNK * 64:
        raise ValueError('Not enough disk space to export the selected images. Free space or select fewer targets.')
    ui.step('Exporting selected Docker images')
    ui.detail('Services', ', '.join(targets))
    ui.info('Cluster configuration and source snapshots are excluded.')
    run(['docker', 'image', 'save', '-o', archive, *[entry['id'] for entry in images.values()]])
    ui.step('Calculating bundle integrity checksum')
    manifest = {'format': 1, 'release': release, 'images': images, 'repositories': repositories,
                'archive': {'size': archive.stat().st_size, 'sha256': digest(archive), 'unpacked_size': estimate}}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    ui.success(f'Shared bundle ready: {archive.stat().st_size / CHUNK:.1f} MiB')
    return manifest


def serve(directory, address, network, port, minutes, fingerprint, stop_file, advertise_address=None):
    token = secrets.token_urlsafe(32)
    deadline = time.monotonic() + minutes * 60
    stopped = threading.Event()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(directory / 'cert.pem', directory / 'key.pem')

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if stopped.is_set() or time.monotonic() >= deadline or stop_file.exists():
                self.send_error(410)
                return
            path, _, provided = self.path.rpartition('/')
            if not hmac.compare_digest(path, '/' + token) or provided not in {'manifest', 'images'}:
                self.send_error(404)
                return
            source = directory / ('manifest.json' if provided == 'manifest' else 'images.tar')
            self.send_response(200)
            self.send_header('Content-Length', str(source.stat().st_size))
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            ui.info(f'Authorized peer {self.client_address[0]} downloading {provided}')
            try:
                with source.open('rb') as stream:
                    while not stopped.is_set() and time.monotonic() < deadline and not stop_file.exists():
                        block = stream.read(CHUNK)
                        if not block:
                            break
                        self.wfile.write(block)
            except (OSError, TimeoutError):
                ui.warning('Peer disconnected; they can retry Pull Shared while this share is active.')

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        slots = threading.BoundedSemaphore(4)

        def process_request(self, connection, peer):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(connection)
                return
            try:
                super().process_request(connection, peer)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, connection, peer):
            try:
                super().process_request_thread(connection, peer)
            finally:
                self.slots.release()

        def get_request(self):
            connection, peer = self.socket.accept()
            connection.settimeout(10)
            if ipaddress.ip_address(peer[0]) not in network:
                connection.close()
                raise OSError('Connection refused: peer is outside the selected LAN subnet')
            try:
                return context.wrap_socket(connection, server_side=True), peer
            except BaseException:
                connection.close()
                raise

    try:
        server = Server((address, port), Handler)
    except OSError as error:
        raise ValueError(f'Cannot listen on {address}:{port}. Check the address and whether the port is in use; '
                         'choose another --share-port if needed.') from error
    server.timeout = 0.5
    try:
        published_address = str(private_address(advertise_address or address))
        ui.share_link(f'https://{published_address}:{server.server_port}/{token}#sha256={fingerprint}')
        ui.detail('Expires in', f'{minutes} minutes')
        ui.detail('LAN endpoint', f'{published_address}:{server.server_port}')
        if published_address != address:
            ui.detail('WSL listener', f'{address}:{server.server_port}')
        ui.success('Ready for Pull Shared. Keep this task running.')
        ui.info('Stop Sharing or Ctrl+C ends access. Send the secret link through a trusted channel.')
        ui.warning('Images may contain application code or embedded secrets. Share only with trusted recipients.')
        ui.section('Transfer Activity')
        while time.monotonic() < deadline and not stop_file.exists():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        ui.success('Sharing stopped. Temporary bundle and certificate will be removed.')


def parse_link(link):
    try:
        parsed = urlsplit(link)
        address = str(private_address(parsed.hostname))
        port = parsed.port or 443
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or
                not re.fullmatch(r'/[A-Za-z0-9_-]{43}', parsed.path) or
                not re.fullmatch(r'sha256=[0-9a-f]{64}', parsed.fragment)):
            raise ValueError('Invalid link')
        return address, port, parsed.path, parsed.fragment.removeprefix('sha256=')
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('Paste the complete Image Linker share link, including its #sha256= fingerprint. '
                         'Only private IPv4 HTTPS links are accepted.') from error


def connect(link, resource):
    address, port, path, expected = parse_link(link)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(address, port, timeout=20, context=context)
    try:
        connection.connect()
        actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise ValueError('Certificate fingerprint mismatch. Nothing was downloaded. '
                             'Obtain a fresh link directly from the giver; do not bypass this check.')
        connection.request('GET', path + '/' + resource)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Share is unavailable or the link has expired. Ask the giver to confirm sharing '
                             'is running and send its current link.')
        return connection, response
    except (OSError, http.client.HTTPException) as error:
        connection.close()
        raise ValueError(f'Cannot establish sharing connection to {address}:{port}. '
                         'Confirm the giver task is running, both machines can reach the same LAN, '
                         'the giver firewall allows the port, and WSL/container ports are exposed. '
                         'Guest Wi-Fi/client isolation or VPN routing may also block access. '
                         'A timeout alone does not identify which cause applies.') from error
    except BaseException:
        connection.close()
        raise


def receive(link, directory, release, targets, output, run):
    connection, response = connect(link, 'manifest')
    try:
        raw = response.read(CHUNK + 1)
        if len(raw) > CHUNK:
            raise ValueError('Shared manifest exceeds the size limit')
        manifest = json.loads(raw)
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError('Shared manifest is invalid or its download was interrupted. '
                         'Ask the giver to confirm the share is running and retry Pull Shared.') from error
    finally:
        connection.close()
    try:
        if manifest['format'] != 1 or manifest['release'] != release:
            raise ValueError('Unsupported share format or HRBC release mismatch. Ask for a build matching your cluster.')
        images = manifest['images']
        if not images or set(images) - TARGETS:
            raise ValueError('Manifest contains unexpected services')
        selected = list(images) if targets is None else targets
        if set(selected) - images.keys():
            raise ValueError('Selected services are not in this shared build')
        platform = json.loads(output(['docker', 'info', '--format', '{{json .}}']))
        architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform['Architecture'], platform['Architecture'])
        for entry in images.values():
            if (not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['id']) or entry['os'] != platform['OSType'] or
                    entry['architecture'] != architecture):
                raise ValueError('Shared image platform is incompatible with your Docker daemon, or image ID is invalid')
        archive_info = manifest['archive']
        size, unpacked = archive_info['size'], archive_info['unpacked_size']
        if (type(size) is not int or type(unpacked) is not int or size <= 0 or unpacked <= 0 or
                not re.fullmatch(r'[0-9a-f]{64}', archive_info['sha256'])):
            raise ValueError('Invalid shared archive metadata')
        if shutil.disk_usage(directory).free < size + unpacked + CHUNK * 64:
            raise ValueError('Insufficient download/import disk space. Free space and retry Pull Shared.')
        repositories = manifest['repositories']
        for key in selected:
            for source in ([key, 'client'] if key == 'ui' else [key]):
                meta = repositories[source]
                if (not isinstance(meta['repository'], str) or not isinstance(meta['branch'], str) or
                        not re.fullmatch(r'[0-9a-f]{40,64}', meta['sha']) or type(meta['dirty']) is not bool):
                    raise ValueError('Invalid source provenance in shared manifest')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('Invalid shared build manifest') from error
    ui.success('Verified sender certificate, cluster release and image platforms')
    ui.detail('Services', ', '.join(selected))
    ui.detail('Bundle size', f'{size / CHUNK:.1f} MiB')
    ui.section('Download')
    ui.progress(0, size)
    archive = directory / 'images.tar'
    connection, response = connect(link, 'images')
    checksum, received = hashlib.sha256(), 0
    last_report = time.monotonic()
    try:
        if response.getheader('Content-Length') != str(size):
            raise ValueError('Shared archive size differs from the manifest')
        with archive.open('wb') as stream:
            while True:
                block = response.read(min(CHUNK, size - received + 1))
                if not block:
                    break
                received += len(block)
                if received > size:
                    raise ValueError('Shared archive exceeds its declared size')
                stream.write(block)
                checksum.update(block)
                if time.monotonic() - last_report >= 2:
                    ui.progress(received, size)
                    last_report = time.monotonic()
        if received != size or not hmac.compare_digest(checksum.hexdigest(), archive_info['sha256']):
            raise ValueError('Download is incomplete or checksum failed. Nothing was imported; retry with a fresh share link.')
    except (OSError, http.client.HTTPException) as error:
        raise ValueError('Download interrupted. Check giver connectivity and expiry, then retry Pull Shared. '
                         'Saved deployment image selections are unchanged.') from error
    finally:
        connection.close()
    ui.progress(received, size)
    ui.success('Bundle size and checksum verified')
    ui.section('Import Images')
    ui.step('Loading images into Docker; no containers will be replaced')
    try:
        run(['docker', 'image', 'load', '-i', archive])
    except subprocess.CalledProcessError as error:
        raise ValueError('Docker could not load the images. Check its output above, Docker storage free space '
                         '(including Docker Desktop disk limits), and daemon access. '
                         'Deployment image selections are unchanged.') from error
    for key, entry in images.items():
        details = json.loads(output(['docker', 'image', 'inspect', entry['id']]))[0]
        if (details['Id'] != entry['id'] or details['Os'] != entry['os'] or details['Architecture'] != entry['architecture'] or
            details.get('Variant', '') != entry.get('variant', '')):
            raise ValueError('Loaded image does not match the manifest; deployment selections are unchanged')
    return manifest, selected