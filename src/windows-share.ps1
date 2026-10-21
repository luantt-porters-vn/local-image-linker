[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SessionDirectory,
    [Parameter(Mandatory = $true)][string]$LanAddress,
    [Parameter(Mandatory = $true)][string]$WslAddress,
    [Parameter(Mandatory = $true)][ValidateRange(1024, 65535)][int]$Port,
    [Parameter(Mandatory = $true)][ValidateRange(1, 1440)][int]$Minutes
)

$ErrorActionPreference = 'Stop'
$ruleCreated = $false
$proxyCreated = $false
$sessionValid = $false
$proxyKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp'
$proxyName = "$LanAddress/$Port"
$proxyValue = "$WslAddress/$Port"

function Write-Status([string]$State, [string]$Message) {
    $status = @{ state = $State; message = $Message } | ConvertTo-Json -Compress
    $temporary = Join-Path $SessionDirectory 'status.tmp'
    [IO.File]::WriteAllText($temporary, $status)
    Move-Item -LiteralPath $temporary -Destination (Join-Path $SessionDirectory 'status.json') -Force
}

function Assert-PrivateIPv4([string]$Value) {
    $address = [Net.IPAddress]::Parse($Value)
    if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Only IPv4 forwarding is supported.'
    }
    $bytes = $address.GetAddressBytes()
    if (-not (($bytes[0] -eq 10) -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168))) {
        throw 'Only private LAN and WSL addresses are permitted.'
    }
}

try {
    $fullDirectory = [IO.Path]::GetFullPath($SessionDirectory).TrimEnd('\')
    $parent = [IO.Path]::GetDirectoryName($fullDirectory)
    $expectedParent = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
    $sessionName = [IO.Path]::GetFileName($fullDirectory)
    if ($parent -ne $expectedParent -or $sessionName -notmatch '^LocalImageLinker-[a-f0-9]{32}$') {
        throw 'Invalid sharing helper directory. Approve elevation using the same Windows account.'
    }
    $sessionValid = $true
    $ruleName = $sessionName
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Windows administrator approval is required for LAN forwarding.'
    }
    Assert-PrivateIPv4 $LanAddress
    Assert-PrivateIPv4 $WslAddress
    $lan = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanAddress -ErrorAction SilentlyContinue)
    if ($lan.Count -ne 1 -or $lan[0].PrefixLength -lt 8 -or $lan[0].PrefixLength -gt 30) {
        throw 'The selected LAN address is no longer assigned to a supported Windows interface.'
    }
    $octets = ([Net.IPAddress]::Parse($LanAddress)).GetAddressBytes()
    $minimumPrefix = if ($octets[0] -eq 10) { 8 } elseif ($octets[0] -eq 172) { 12 } else { 16 }
    if ($lan[0].PrefixLength -lt $minimumPrefix) {
        throw 'The Windows LAN subnet extends beyond private address space. Use a correctly scoped LAN interface.'
    }
    $remaining = [int]$lan[0].PrefixLength
    for ($position = 0; $position -lt 4; $position++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $remaining))
        $mask = if ($bits -eq 0) { 0 } else { (255 -shl (8 - $bits)) -band 255 }
        $octets[$position] = $octets[$position] -band $mask
        $remaining -= 8
    }
    $subnet = "$($octets -join '.')/$($lan[0].PrefixLength)"
    $mapping = Get-ItemProperty -LiteralPath $proxyKey -ErrorAction SilentlyContinue
    if ($null -ne $mapping -and $null -ne $mapping.PSObject.Properties[$proxyName]) {
        throw 'A forwarding rule already owns this address and port. Remove the old manual rule or choose another sharing port.'
    }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @($LanAddress, '0.0.0.0', '::') })
    if ($listeners.Count -gt 0) {
        throw 'The Windows LAN port is already in use. Stop its owner or choose another sharing port.'
    }
    $ipHelper = Get-Service -Name iphlpsvc
    if ($ipHelper.Status -ne 'Running') {
        throw 'Windows IP Helper service is not running. Start it through Windows Services, then retry sharing.'
    }
    New-NetFirewallRule -Name $ruleName -DisplayName "Local Image Linker temporary share ($Port)" `
        -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $LanAddress -LocalPort $Port `
        -RemoteAddress $subnet -InterfaceAlias $lan[0].InterfaceAlias -Profile Any -Enabled True | Out-Null
    $ruleCreated = $true
    & netsh.exe interface portproxy add v4tov4 "listenaddress=$LanAddress" "listenport=$Port" `
        "connectaddress=$WslAddress" "connectport=$Port" protocol=tcp | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Windows could not create the temporary TCP forwarding rule.'
    }
    $proxyCreated = $true
    $mapping = Get-ItemProperty -LiteralPath $proxyKey -ErrorAction SilentlyContinue
    if ($null -eq $mapping -or $mapping.$proxyName -ne $proxyValue) {
        throw 'Windows forwarding could not be verified.'
    }
    Write-Status 'ready' "LAN access ready on ${LanAddress}:$Port; firewall scope $subnet."
    $deadline = [DateTime]::UtcNow.AddMinutes($Minutes + 2)
    $leaseChanged = [DateTime]::UtcNow
    $previousLease = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath (Join-Path $SessionDirectory 'stop')) { break }
        $lease = Get-Content -LiteralPath (Join-Path $SessionDirectory 'lease') -Raw -ErrorAction SilentlyContinue
        if ($lease -ne $previousLease) {
            $previousLease = $lease
            $leaseChanged = [DateTime]::UtcNow
        }
        if (([DateTime]::UtcNow - $leaseChanged).TotalSeconds -gt 30) { break }
        Start-Sleep -Milliseconds 500
    }
}
catch {
    if ($sessionValid) { Write-Status 'error' $_.Exception.Message }
    exit 1
}
finally {
    $cleanupErrors = @()
    if ($proxyCreated) {
        try {
            $mapping = Get-ItemProperty -LiteralPath $proxyKey -ErrorAction SilentlyContinue
            if ($null -ne $mapping -and $mapping.$proxyName -eq $proxyValue) {
                & netsh.exe interface portproxy delete v4tov4 "listenaddress=$LanAddress" "listenport=$Port" protocol=tcp | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Forwarding removal failed.' }
            }
        }
        catch { $cleanupErrors += $_.Exception.Message }
    }
    if ($ruleCreated) {
        try { Remove-NetFirewallRule -Name $ruleName -ErrorAction Stop }
        catch { $cleanupErrors += $_.Exception.Message }
    }
    if ($sessionValid) {
        if ($cleanupErrors.Count -gt 0) {
            Write-Status 'cleanup-error' ($cleanupErrors -join ' ')
        }
        elseif (Test-Path -LiteralPath (Join-Path $SessionDirectory 'status.json')) {
            $current = Get-Content -LiteralPath (Join-Path $SessionDirectory 'status.json') -Raw | ConvertFrom-Json
            if ($current.state -eq 'error') {
                Write-Status 'failed' $current.message
            }
            else {
                Write-Status 'stopped' 'Temporary Windows forwarding and firewall rules removed.'
            }
        }
    }
}