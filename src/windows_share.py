"""Manage explicitly approved, temporary Windows LAN access for WSL NAT shares."""
import base64
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from feature_share import PRIVATE_NETWORKS, private_address
from terminal_ui import ui


POWERSHELL = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
NETWORK_QUERY = r'''
$interfaces = @(Get-NetIPConfiguration | Where-Object {
    $_.NetAdapter.Status -eq 'Up' -and $_.IPv4DefaultGateway
} | ForEach-Object {
    $configuration = $_
    foreach ($address in $_.IPv4Address) {
        [PSCustomObject]@{
            address = $address.IPAddress
            prefix = $address.PrefixLength
            interface = $configuration.InterfaceAlias
        }
    }
})
ConvertTo-Json -InputObject $interfaces -Compress
'''


def powershell(script, timeout=30):
    executable = shutil.which('powershell.exe') or str(POWERSHELL)
    preamble = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
    preamble += '[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); '
    encoded = base64.b64encode((preamble + script).encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
                                errors='replace', timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Windows sharing setup could not complete. Approve the Windows administrator prompt '
                         'using the same Windows account. If you cancelled it, rerun Share Last Build. '
                         'Ensure Windows PowerShell and WSL interoperability are available.') from error
    return result.stdout.strip().lstrip('\ufeff')


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def networking_mode():
    if 'microsoft' not in Path('/proc/sys/kernel/osrelease').read_text().lower():
        return 'native'
    executable = shutil.which('wslinfo')
    if executable:
        result = subprocess.run([executable, '--networking-mode'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip().lower()
    return 'unknown'


def windows_interface():
    try:
        values = json.loads(powershell(NETWORK_QUERY))
        candidates = []
        for entry in values if isinstance(values, list) else [values]:
            try:
                address = str(private_address(entry['address']))
                prefix = entry['prefix']
                if type(prefix) is not int or not 8 <= prefix <= 30:
                    continue
                network = ipaddress.ip_network(f'{address}/{prefix}', strict=False)
                if not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
                    continue
                candidates.append((entry['interface'], address, network))
            except (ValueError, KeyError, TypeError):
                continue
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError('Windows did not return usable network information. Check the Wi-Fi connection and retry.') from error
    if not candidates:
        raise ValueError('No private Windows LAN interface with a default gateway was found. '
                         'Connect to the local network and retry sharing.')
    if len(candidates) > 1:
        for name, address, subnet in candidates:
            ui.detail(name, f'{address} | {subnet}')
        if not os.isatty(0):
            raise ValueError('Several Windows networks are active. Run sharing in an interactive terminal to select the LAN.')
        chosen = ui.prompt('Enter the Windows LAN IPv4 address coworkers can reach')
        candidates = [entry for entry in candidates if entry[1] == chosen]
        if not candidates:
            raise ValueError('Choose one of the displayed Windows LAN addresses')
    return candidates[0]


def read_status(directory):
    try:
        status = json.loads((directory / 'status.json').read_text(encoding='utf-8-sig'))
        return status if isinstance(status, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@contextmanager
def expose(address, port, minutes, stop_file):
    mode = networking_mode()
    if mode in {'native', 'mirrored', 'bridged'}:
        yield address
        return
    ui.section('Windows LAN Access')
    ui.detail('WSL mode', mode)
    name, lan_address, subnet = windows_interface()
    if lan_address == address:
        yield address
        return
    ui.detail('Windows LAN', f'{name} | {lan_address}')
    ui.detail('Allowed LAN', subnet)
    ui.warning('Windows will ask for administrator approval to create temporary forwarding and firewall rules.')
    ui.info('Approve the UAC prompt. Nothing is installed and the firewall is not disabled.')
    session_name = 'LocalImageLinker-' + uuid.uuid4().hex
    native_directory = powershell(
        f'$directory = Join-Path $env:TEMP {quote(session_name)}; '
        'New-Item -ItemType Directory -Path $directory | Out-Null; Write-Output $directory')
    try:
        converted = subprocess.run(['wslpath', '-u', native_directory], capture_output=True, text=True,
                                   timeout=10, check=True).stdout.strip()
        directory = Path(converted)
        if directory.name != session_name or not directory.is_dir():
            raise ValueError('Windows temporary sharing directory could not be accessed from WSL')
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Windows temporary storage is not accessible from WSL. Check Windows drive mounting and retry.') from error
    worker = directory / 'windows-share.ps1'
    worker.write_bytes(Path(__file__).with_name('windows-share.ps1').read_bytes())
    (directory / 'lease').write_text('starting')
    finished = threading.Event()
    activated = threading.Event()

    def heartbeat():
        while not finished.is_set():
            try:
                (directory / 'lease').write_text(str(time.monotonic()))
                status = read_status(directory)
                if activated.is_set() and status.get('state') not in {None, 'ready'}:
                    ui.warning('Windows forwarding ended. Stopping this share: ' + status.get('message', 'helper stopped'))
                    stop_file.touch()
                    return
            except OSError:
                ui.warning('Lost access to the Windows sharing helper. Stopping this share.')
                stop_file.touch()
                return
            finished.wait(2)

    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    launched = False
    try:
        invocation = (f'& {quote(native_directory + chr(92) + "windows-share.ps1")} '
                      f'-SessionDirectory {quote(native_directory)} -LanAddress {quote(lan_address)} '
                      f'-WslAddress {quote(address)} -Port {port} -Minutes {minutes}')
        encoded_worker = base64.b64encode(invocation.encode('utf-16le')).decode('ascii')
        launcher = ("$process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden "
                '-WorkingDirectory $env:SystemRoot '
                    "-ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',"
                    f"'-EncodedCommand',{quote(encoded_worker)}) -PassThru; Write-Output $process.Id")
        powershell(launcher, timeout=180)
        launched = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = read_status(directory)
            if status.get('state') == 'ready':
                break
            if status.get('state') in {'error', 'failed', 'cleanup-error'}:
                raise ValueError('Windows LAN setup failed: ' + status.get('message', 'unknown helper error'))
            if stop_file.exists():
                raise ValueError('Sharing was stopped during Windows LAN setup')
            finished.wait(0.2)
        else:
            raise ValueError('Windows sharing helper did not become ready. Check Windows security restrictions and retry.')
        activated.set()
        ui.success(f'Windows forwarding ready on {lan_address}:{port}')
        ui.info('The share link will use the Windows LAN address. Temporary rules are removed when sharing ends.')
        yield lan_address
    finally:
        try:
            (directory / 'stop').touch()
        except OSError:
            ui.warning('Could not signal Windows cleanup. Its watchdog will stop when the heartbeat expires.')
        finally:
            finished.set()
            monitor.join(5)
        if launched:
            deadline = time.monotonic() + 15
            cleanup_wait = threading.Event()
            while time.monotonic() < deadline:
                status = read_status(directory)
                if status.get('state') in {'stopped', 'failed', 'cleanup-error'}:
                    break
                cleanup_wait.wait(0.2)
            if status.get('state') in {'stopped', 'failed'}:
                ui.success('Temporary Windows forwarding and firewall rules removed')
                shutil.rmtree(directory)
            else:
                ui.warning('Windows cleanup could not be confirmed. Helper records are kept for recovery.')
                ui.detail('Helper files', native_directory)
                ui.detail('Owned rule', session_name)
                ui.detail('Forwarding', f'{lan_address}:{port} -> {address}:{port}')
        else:
            ui.warning('Windows setup was not confirmed. Any started helper will stop after its heartbeat expires.')