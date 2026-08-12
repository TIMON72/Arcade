# Wipe Arcade sync + runtime stubs on a Batocera cabinet (before a clean deploy).
# Usage:
#   .\deploy\clean.ps1 zero
#   .\deploy\clean.ps1 zero -Runtime   # also remove /userdata/system/scripts/{main,timer,server,tvon}
#   .\deploy\clean.ps1 zero -Venv      # + remove .venv (next Full deploy recreates it)

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,
    [string]$SshUser,
    [string]$Auth,
    [string]$RemotePath,
    [switch]$Runtime,
    [switch]$Venv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-MachinesConfig {
    $path = Join-Path $PSScriptRoot 'machines.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Test-DeployIpv4Name {
    param([string]$Name)
    $parts = $Name.Split('.')
    if ($parts.Count -ne 4) {
        return $false
    }
    foreach ($part in $parts) {
        if ($part.Length -lt 1 -or $part.Length -gt 3) {
            return $false
        }
        foreach ($ch in $part.ToCharArray()) {
            if ($ch -lt '0' -or $ch -gt '9') {
                return $false
            }
        }
        $n = [int]$part
        if ($n -lt 0 -or $n -gt 255) {
            return $false
        }
    }
    return $true
}

function Resolve-TargetHost {
    param(
        [string]$Name,
        $Config
    )
    if (Test-DeployIpv4Name -Name $Name) {
        return $Name
    }
    if ($null -eq $Config -or $null -eq $Config.machines) {
        throw "Unknown target $Name. Use an IP or deploy/machines.json."
    }
    $prop = $Config.machines.PSObject.Properties[$Name]
    if ($null -eq $prop) {
        $known = @($Config.machines.PSObject.Properties.Name) -join ', '
        throw "Unknown machine $Name. Known: $known"
    }
    return [string]$prop.Value
}

function New-AskPassScript {
    param([string]$Text)
    $askCmd = Join-Path $env:TEMP ('arcade-askpass-clean-' + $PID + '.cmd')
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-clean-' + $PID + '.ps1')
    $ps = '[Console]::Out.Write(' + ("'" + $Text.Replace("'", "''") + "'") + ')'
    Set-Content -LiteralPath $askPs1 -Value $ps -Encoding ASCII
    Set-Content -LiteralPath $askCmd -Value @(
        '@echo off',
        ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + $askPs1 + '"')
    ) -Encoding ASCII
    return $askCmd
}

$config = Read-MachinesConfig
$cabinetHost = Resolve-TargetHost -Name $Target -Config $config

if (-not $SshUser) {
    if ($config -and $config.user) {
        $SshUser = [string]$config.user
    }
    else {
        $SshUser = 'root'
    }
}
if (-not $RemotePath) {
    if ($config -and $config.remotePath) {
        $RemotePath = [string]$config.remotePath
    }
    else {
        $RemotePath = '/userdata/system/Arcade'
    }
}
if (-not $PSBoundParameters.ContainsKey('Auth')) {
    if ($env:ARCADE_SSH_PASSWORD) {
        $Auth = $env:ARCADE_SSH_PASSWORD
    }
    elseif ($config -and $null -ne $config.password) {
        $Auth = [string]$config.password
    }
    else {
        $Auth = 'linux'
    }
}

Write-Host ('... clearing SSH host keys for ' + $cabinetHost)
$prevEa = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & ssh-keygen -R $cabinetHost 1>$null 2>$null
    & ssh-keygen -R ('[' + $cabinetHost + ']:22') 1>$null 2>$null
}
finally {
    $ErrorActionPreference = $prevEa
}

$sshTarget = $SshUser + '@' + $cabinetHost
$sshBaseArgs = @(
    '-o', ('HostName=' + $cabinetHost),
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=NUL',
    '-o', 'GlobalKnownHostsFile=NUL',
    '-o', 'UpdateHostKeys=no',
    '-o', 'PreferredAuthentications=password',
    '-o', 'PubkeyAuthentication=no',
    '-o', 'ConnectTimeout=20',
    '-o', 'LogLevel=ERROR'
)

$askCmd = New-AskPassScript -Text $Auth
$runtimeFlag = if ($Runtime) { '1' } else { '0' }
$venvFlag = if ($Venv) { '1' } else { '0' }

# Remote shell: stop services first so open files cannot leave 0-byte stubs.
$remote = @'
set -e
RP='__REMOTE__'
RUNTIME=__RUNTIME__
VENV=__VENV__
echo "=== stop arcade services ==="
for s in main timer server tvon; do
  batocera-services stop "$s" 2>/dev/null || true
done
killall -q cec-client 2>/dev/null || true
pkill -f '/userdata/system/scripts/.*/(main|timer|server|tvon)\.py' 2>/dev/null || true
pkill -f '/userdata/system/Arcade/scripts/.*/(main|timer|server|tvon)\.py' 2>/dev/null || true
sleep 1

echo "=== wipe checkout trees under $RP ==="
rm -rf "$RP/scripts" "$RP/services" "$RP/configs"
rm -f /userdata/system/.arcade-deployed

if [ "$RUNTIME" = "1" ]; then
  echo "=== wipe runtime packages under /userdata/system/scripts ==="
  rm -rf /userdata/system/scripts/main \
         /userdata/system/scripts/timer \
         /userdata/system/scripts/server \
         /userdata/system/scripts/tvon
  rm -f /userdata/system/services/main \
        /userdata/system/services/timer \
        /userdata/system/services/server \
        /userdata/system/services/tvon
fi

if [ "$VENV" = "1" ]; then
  echo "=== wipe venv ==="
  rm -rf /userdata/system/scripts/.venv /userdata/system/scripts/venv "$RP/.venv"
fi

echo "=== remove zero-byte leftovers (checkout) ==="
if [ -d "$RP" ]; then
  find "$RP" -type f -size 0 -print -delete 2>/dev/null || true
fi
echo "=== remove zero-byte leftovers (runtime py/services) ==="
find /userdata/system/scripts /userdata/system/services -type f -size 0 \
  \( -name '*.py' -o -name 'main' -o -name 'timer' -o -name 'server' -o -name 'tvon' -o -name '*.toml' \) \
  -print -delete 2>/dev/null || true

sync
echo "=== remaining empty critical files ==="
find /userdata/system/scripts /userdata/system/services "$RP" -type f -size 0 2>/dev/null \
  | grep -E 'scripts/(main|timer|server|tvon)|/services/(main|timer|server|tvon)$' || echo '(none)'
echo "OK clean done on $(hostname) / $RP"
echo "Next: .\\deploy\\deploy.ps1 $TARGET -Full -Restart"
'@
$remote = $remote.Replace('__REMOTE__', $RemotePath).Replace('__RUNTIME__', $runtimeFlag).Replace('__VENV__', $venvFlag).Replace('$TARGET', $Target)

try {
    Write-Host ('-> clean ' + $sshTarget + ' path=' + $RemotePath + ' Runtime=' + $Runtime + ' Venv=' + $Venv)
    $prevAsk = $env:SSH_ASKPASS
    $prevReq = $env:SSH_ASKPASS_REQUIRE
    $prevDisp = $env:DISPLAY
    $env:SSH_ASKPASS = $askCmd
    $env:SSH_ASKPASS_REQUIRE = 'force'
    $env:DISPLAY = '1'
    $ErrorActionPreference = 'Continue'
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
    & ssh @sshBaseArgs $sshTarget ("echo $b64 | base64 -d | sh")
    if ($LASTEXITCODE -ne 0) {
        throw ('clean failed (exit ' + $LASTEXITCODE + ')')
    }
}
finally {
    $ErrorActionPreference = 'Stop'
    if ($null -eq $prevAsk) { Remove-Item Env:\SSH_ASKPASS -ErrorAction SilentlyContinue } else { $env:SSH_ASKPASS = $prevAsk }
    if ($null -eq $prevReq) { Remove-Item Env:\SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue } else { $env:SSH_ASKPASS_REQUIRE = $prevReq }
    if ($null -eq $prevDisp) { Remove-Item Env:\DISPLAY -ErrorAction SilentlyContinue } else { $env:DISPLAY = $prevDisp }
    Remove-Item -LiteralPath $askCmd -Force -ErrorAction SilentlyContinue
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-clean-' + $PID + '.ps1')
    Remove-Item -LiteralPath $askPs1 -Force -ErrorAction SilentlyContinue
}

Write-Host ('OK cleaned ' + $cabinetHost)
