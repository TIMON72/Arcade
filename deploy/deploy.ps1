# Sync local Arcade tree to a Batocera cabinet, then run on-device deploy.
# Safe path: userdata I/O probe → stop services → /tmp staging → Arcade.new verify →
# swap → main.py deploy (atomic package replace) → runtime size check → optional restart.
# Never wipes live runtime before verified staging/checkout.
# Usage:
#   .\deploy\deploy.ps1 zero -Update          # освежить: sync + deploy + restart сервисов
#   .\deploy\deploy.ps1 zero -Update -Full    # то же + wheels/vendor
#   .\deploy\test.ps1 zero                    # health: boot 5m + alive 5m, x3 with reboot
#   .\deploy\deploy.ps1 zero
#   .\deploy\deploy.ps1 zero -Full -Restart
#   .\deploy\deploy.ps1 zero -NoLogs
#   .\deploy\deploy.ps1 -Copy C:\Temp\Batocera\system
#       → system\Arcade\ (+ wheels/vendor)
#       → system\scripts\{main,timer,server,tvon,wheels}
#       → system\scripts\.venv   (if vendor/venv/aarch64.tar.gz present)
#       → system\configs\
#       → system\services\{main,timer,server,tvon}  (LF)
#       → system\batocera.conf + .arcade-deployed
#   .\deploy\deploy.ps1 -Copy C:\Temp\Batocera\system\Arcade   # same, system = parent
#   .\deploy\deploy.ps1 zero -PullVenv   # fetch remote .venv → vendor/venv/aarch64.tar.gz
#   After copying onto mounted SHARE: .\deploy\mount.ps1 -FixPerms
#   (-Mount also runs FixPerms once at mount time)

[CmdletBinding(DefaultParameterSetName = 'Remote')]
param(
    [Parameter(Mandatory = $true, Position = 0, ParameterSetName = 'Remote')]
    [Parameter(Mandatory = $true, Position = 0, ParameterSetName = 'PullVenv')]
    [string]$Target,

    # Parent folder (creates/updates Arcade inside) or the Arcade folder itself.
    [Parameter(Mandatory = $true, ParameterSetName = 'Copy')]
    [string]$Copy,

    [string]$SshUser,
    [string]$Auth,
    [string]$RemotePath,
    [string]$RepoRoot,
    [switch]$Full,
    [Parameter(ParameterSetName = 'Remote')]
    [switch]$NoDeploy,
    [Parameter(ParameterSetName = 'Remote')]
    [switch]$Restart,
    # Sync new/changed files, on-device deploy, restart Arcade services.
    [Parameter(ParameterSetName = 'Remote')]
    [switch]$Update,
    [Parameter(ParameterSetName = 'Remote')]
    [switch]$NoLogs,
    [Parameter(Mandatory = $true, ParameterSetName = 'PullVenv')]
    [switch]$PullVenv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DeployExcludeArgs {
    param([switch]$IncludeFull)
    $excludeArgs = @(
        '--exclude=.git',
        '--exclude=.venv',
        '--exclude=venv',
        '--exclude=__pycache__',
        '--exclude=logs.log',
        '--exclude=.arcade-deployed',
        '--exclude=.cursor',
        '--exclude=*.pyc',
        '--exclude=deploy/machines.json'
    )
    if (-not $IncludeFull) {
        $excludeArgs += @('--exclude=wheels', '--exclude=vendor')
    }
    return $excludeArgs
}

function Resolve-CopyArcadeDestination {
    param([string]$Path)
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $leaf = Split-Path -Leaf $full
    if ($leaf -ieq 'Arcade') {
        return $full
    }
    return (Join-Path $full 'Arcade')
}

function Write-UnixTextFile {
    param(
        [string]$SourcePath,
        [string]$DestPath
    )
    $text = [System.IO.File]::ReadAllText($SourcePath)
    $text = $text -replace "`r`n", "`n" -replace "`r", "`n"
    if (-not $text.EndsWith("`n")) {
        $text += "`n"
    }
    $dir = Split-Path -Parent $DestPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($DestPath, $text, $utf8NoBom)
}

function Get-ArcadeFingerprint {
    param([string]$SourceRoot)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $rev = & git -C $SourceRoot rev-parse HEAD 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -eq 0 -and -not [string]::IsNullOrWhiteSpace($rev)) {
        return ('git:' + "$rev".Trim())
    }
    $latest = 0.0
    foreach ($folder in @('scripts', 'services')) {
        $root = Join-Path $SourceRoot $folder
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ne '.pyc' -and $_.Name -ne 'logs.log' } |
            ForEach-Object {
                $t = $_.LastWriteTimeUtc.Subtract([datetime]'1970-01-01').TotalSeconds
                if ($t -gt $latest) { $latest = $t }
            }
    }
    return ('mtime:' + [math]::Floor($latest).ToString())
}

function Copy-TreeFiltered {
    param(
        [string]$Source,
        [string]$Dest,
        [string[]]$ExcludeDirs = @('__pycache__', '.venv', 'venv', '.git')
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Missing source tree: $Source"
    }
    if (Test-Path -LiteralPath $Dest) {
        Remove-Item -LiteralPath $Dest -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    # /E copy subdirs; /NFL /NDL /NJH /NJS quiet; /XD excludes
    $xd = @()
    foreach ($d in $ExcludeDirs) { $xd += @('/XD', $d) }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & robocopy $Source $Dest /E /NFL /NDL /NJH /NJS /NC /NS /NP @xd | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    # robocopy: 0-7 success
    if ($code -ge 8) {
        throw "robocopy failed ($code): $Source -> $Dest"
    }
}

function Copy-ArcadeSystemSeed {
    param(
        [string]$SourceRoot,
        [string]$SystemRoot
    )
    $services = @('main', 'timer', 'server', 'tvon')
    $servicesDir = Join-Path $SystemRoot 'services'
    $scriptsDir = Join-Path $SystemRoot 'scripts'
    $configsDir = Join-Path $SystemRoot 'configs'

    New-Item -ItemType Directory -Path $servicesDir -Force | Out-Null
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null

    Write-Host ('... seeding services (LF) -> ' + $servicesDir)
    foreach ($name in $services) {
        $src = Join-Path $SourceRoot ('services\' + $name)
        if (-not (Test-Path -LiteralPath $src)) {
            throw "Missing service script: $src"
        }
        Write-UnixTextFile -SourcePath $src -DestPath (Join-Path $servicesDir $name)
        Write-Host ('    services/' + $name)
    }

    Write-Host ('... seeding runtime scripts -> ' + $scriptsDir)
    foreach ($name in $services) {
        $srcPkg = Join-Path $SourceRoot ('scripts\' + $name)
        if (-not (Test-Path -LiteralPath $srcPkg)) {
            throw "Missing scripts package: $srcPkg"
        }
        $dstPkg = Join-Path $scriptsDir $name
        Copy-TreeFiltered -Source $srcPkg -Dest $dstPkg
        $py = Join-Path $dstPkg ($name + '.py')
        if (-not (Test-Path -LiteralPath $py) -or ((Get-Item -LiteralPath $py).Length -lt 500)) {
            throw "Seeded runtime too small/missing: $py"
        }
        Write-Host ('    scripts/' + $name + '/')
    }

    $wheelsSrc = Join-Path $SourceRoot 'wheels'
    if (Test-Path -LiteralPath $wheelsSrc) {
        $wheelsDst = Join-Path $scriptsDir 'wheels'
        Write-Host ('... seeding scripts/wheels -> ' + $wheelsDst)
        Copy-TreeFiltered -Source $wheelsSrc -Dest $wheelsDst
    }

    $configsSrc = Join-Path $SourceRoot 'configs'
    if (Test-Path -LiteralPath $configsSrc) {
        Write-Host ('... seeding configs -> ' + $configsDir)
        Copy-TreeFiltered -Source $configsSrc -Dest $configsDir
    }

    $confSrc = Join-Path $SourceRoot 'batocera.conf'
    if (-not (Test-Path -LiteralPath $confSrc)) {
        throw "Missing batocera.conf: $confSrc"
    }
    $confDst = Join-Path $SystemRoot 'batocera.conf'
    Write-Host ('... seeding batocera.conf -> ' + $confDst)
    Write-UnixTextFile -SourcePath $confSrc -DestPath $confDst

    $fp = Get-ArcadeFingerprint -SourceRoot $SourceRoot
    $marker = Join-Path $SystemRoot '.arcade-deployed'
    $markerBody = "/userdata/system/Arcade`n$fp`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($marker, $markerBody.Replace("`r`n", "`n"), $utf8NoBom)
    Write-Host ('... marker ' + $marker + ' (' + $fp + ')')

    Expand-VendorVenvIntoScripts -ArcadeRoot (Join-Path $SystemRoot 'Arcade') -ScriptsRoot (Join-Path $SystemRoot 'scripts')
}

function Expand-VendorVenvIntoScripts {
    param(
        [string]$ArcadeRoot,
        [string]$ScriptsRoot
    )
    $archive = Join-Path $ArcadeRoot 'vendor\venv\aarch64.tar.gz'
    if (-not (Test-Path -LiteralPath $archive)) {
        Write-Host '... vendor/venv/aarch64.tar.gz not found — first boot will create .venv on device'
        return
    }
    $venvDest = Join-Path $ScriptsRoot '.venv'
    Write-Host ('... seeding prebuilt .venv from ' + $archive)
    if (Test-Path -LiteralPath $venvDest) {
        Remove-Item -LiteralPath $venvDest -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ScriptsRoot -Force | Out-Null
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & tar -xzf $archive -C $ScriptsRoot
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) {
        throw ('tar extract venv failed (exit ' + $code + '): ' + $archive)
    }
    $py = Join-Path $venvDest 'bin\python'
    if (-not (Test-Path -LiteralPath $py)) {
        throw ('extracted venv missing bin/python: ' + $py)
    }
    Write-Host ('... prebuilt .venv ready -> ' + $venvDest)
}

function Copy-ArcadeTree {
    param(
        [string]$SourceRoot,
        [string]$DestArcade
    )

    $systemRoot = Split-Path -Parent $DestArcade
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        throw "Cannot resolve system root from $DestArcade"
    }

    # Offline/manual SHARE copy: always ship wheels+vendor (same as remote -Full).
    $excludeArgs = Get-DeployExcludeArgs -IncludeFull
    $tarPath = Join-Path $env:TEMP ('arcade-copy-' + $PID + '.tar')

    Write-Host ('-> copy pack from ' + $SourceRoot)
    Write-Host ('-> system root    ' + $systemRoot)
    Write-Host ('-> Arcade dest    ' + $DestArcade)
    Write-Host '... packing (wheels+vendor included)...'

    try {
        & tar --format=ustar -cf $tarPath @excludeArgs -C $SourceRoot .
        if ($LASTEXITCODE -ne 0) {
            throw ('tar create failed (exit ' + $LASTEXITCODE + ')')
        }

        $sizeMb = [math]::Round((Get-Item -LiteralPath $tarPath).Length / 1MB, 2)
        Write-Host ('... extracting ' + $sizeMb + ' MB...')

        if (Test-Path -LiteralPath $DestArcade) {
            Remove-Item -LiteralPath $DestArcade -Recurse -Force
        }
        New-Item -ItemType Directory -Path $DestArcade -Force | Out-Null

        & tar -xf $tarPath -C $DestArcade
        if ($LASTEXITCODE -ne 0) {
            throw ('tar extract failed (exit ' + $LASTEXITCODE + ')')
        }
    }
    finally {
        Remove-Item -LiteralPath $tarPath -Force -ErrorAction SilentlyContinue
    }

    $mainPy = Join-Path $DestArcade 'scripts\main\main.py'
    $tvonPy = Join-Path $DestArcade 'scripts\tvon\tvon.py'
    if (-not (Test-Path -LiteralPath $mainPy)) {
        throw "Copy missing scripts/main/main.py under $DestArcade"
    }
    if (-not (Test-Path -LiteralPath $tvonPy)) {
        throw "Copy missing scripts/tvon/tvon.py under $DestArcade"
    }

    Copy-ArcadeSystemSeed -SourceRoot $SourceRoot -SystemRoot $systemRoot

    Write-Host ''
    Write-Host ('OK first-boot bundle ready under: ' + $systemRoot)
    Write-Host '  Arcade/            checkout (+ wheels/vendor)'
    Write-Host '  scripts/           main timer server tvon + wheels  (runtime)'
    Write-Host '  configs/           batocera configs'
    Write-Host '  services/          main timer server tvon  (LF)'
    Write-Host '  batocera.conf      system.services=...'
    Write-Host '  .arcade-deployed   marker'
    Write-Host '  scripts/.venv      if vendor/venv/aarch64.tar.gz was present'
    Write-Host ''

    # If SSD boot is already mounted in WSL, patch cmdline now (Pi5+NVMe).
    $bootCmd = $null
    try {
        $distro = Get-DefaultWslDistroForDeploy
        $nvme = Update-BatoceraNvmeCmdlineViaWsl -Distro $distro
        Write-Host ($nvme.Output -replace "`n", ' | ')
        if ($nvme.Output -match 'CMDLINE_(PATCHED|ALREADY_OK)') {
            $bootCmd = $true
        }
    } catch {
        Write-Host ('... boot cmdline not patched yet: ' + $_.Exception.Message)
    }
    if (-not $bootCmd) {
        Write-Host '... boot cmdline: will be auto-patched on next .\deploy\mount.ps1 -Mount'
    }

    Write-Host ''
    Write-Host 'Copy the whole system\ tree onto SHARE (not only Arcade/).'
    Write-Host '  .\deploy\mount.ps1 -Mount       # mount + Explorer perms + NVMe cmdline'
    Write-Host '  (paste system\ in Explorer)'
    Write-Host '  .\deploy\mount.ps1 -FixPerms    # after paste (services +x)'
    Write-Host 'Then boot — services should start without manual install.'
    Write-Host 'Tip: unique system.hostname to avoid batocera.local clashes.'
    Write-Host 'Tip: seed venv once: .\deploy\deploy.ps1 <machine> -PullVenv'
}

function Get-DefaultWslDistroForDeploy {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $raw = & wsl -l -q 2>&1 | Out-String
    $ErrorActionPreference = $prev
    $text = ($raw -replace "`0", '').Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw 'WSL not available'
    }
    $first = ($text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if (-not $first) { throw 'No WSL distro' }
    return $first.Trim()
}

function Update-BatoceraNvmeCmdlineViaWsl {
    param(
        [string]$Distro,
        [string]$BootMount = '/mnt/wsl/batocera-boot'
    )
    $bash = @"
set -e
BOOT='$BootMount'
FILE="`$BOOT/cmdline.txt"
if ! mountpoint -q "`$BOOT"; then
  echo BOOT_NOT_MOUNTED
  exit 3
fi
mount -o remount,rw "`$BOOT" 2>/dev/null || true
if [ ! -f "`$FILE" ]; then
  echo NO_CMDLINE
  exit 4
fi
BEFORE=`$(tr -d '\r\n' < "`$FILE")
AFTER="`$BEFORE"
for tok in nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off; do
  case " `$AFTER " in
    *" `$tok "*) ;;
    *) AFTER="`$AFTER `$tok" ;;
  esac
done
if [ "`$AFTER" = "`$BEFORE" ]; then
  echo CMDLINE_ALREADY_OK
  echo "`$AFTER"
  exit 0
fi
cp -a "`$FILE" "`$FILE.bak.nvme" 2>/dev/null || true
printf '%s\n' "`$AFTER" > "`$FILE"
sync
echo CMDLINE_PATCHED
echo "`$AFTER"
"@
    $tmp = Join-Path $env:TEMP ('arcade-nvme-cmdline-' + [guid]::NewGuid().ToString('N') + '.sh')
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($tmp, ($bash -replace "`r`n", "`n" -replace "`r", "`n"), $utf8NoBom)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $wslPath = & wsl -d $Distro -e wslpath -a $tmp 2>&1
        if ($LASTEXITCODE -ne 0) { throw "wslpath failed: $wslPath" }
        $wslPath = (($wslPath | Out-String) -replace "`0", '').Trim()
        $out = & wsl -d $Distro -u root -e bash $wslPath 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        $text = (($out | ForEach-Object { "$_" }) -join "`n") -replace "`0", ''
        return [pscustomobject]@{ ExitCode = $code; Output = $text.TrimEnd() }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

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
        if ($part.Length -lt 1) {
            return $false
        }
        if ($part.Length -gt 3) {
            return $false
        }
        foreach ($ch in $part.ToCharArray()) {
            if ($ch -lt '0') {
                return $false
            }
            if ($ch -gt '9') {
                return $false
            }
        }
        $n = [int]$part
        if ($n -lt 0) {
            return $false
        }
        if ($n -gt 255) {
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
    if ($null -eq $Config) {
        throw "Unknown target $Name. Use an IP or deploy/machines.json."
    }
    if ($null -eq $Config.machines) {
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
    $askCmd = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.cmd')
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.ps1')
    # Без CR/LF: echo/set /p под OpenSSH ASKPASS на Windows часто ломают пароль.
    $ps = '[Console]::Out.Write(' + ("'" + $Text.Replace("'", "''") + "'") + ')'
    Set-Content -LiteralPath $askPs1 -Value $ps -Encoding ASCII
    $line1 = '@echo off'
    $line2 = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $askPs1 + '"'
    Set-Content -LiteralPath $askCmd -Value @($line1, $line2) -Encoding ASCII
    return $askCmd
}

function Invoke-SshNative {
    param(
        [string]$Binary,
        $Arguments,
        [string]$AskCmd,
        [string]$FailMessage
    )
    $prevAsk = $env:SSH_ASKPASS
    $prevReq = $env:SSH_ASKPASS_REQUIRE
    $prevDisp = $env:DISPLAY
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($AskCmd) {
            $env:SSH_ASKPASS = $AskCmd
            $env:SSH_ASKPASS_REQUIRE = 'force'
            $env:DISPLAY = '1'
        }
        & $Binary @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw ($FailMessage + ' (exit ' + $LASTEXITCODE + ')')
        }
    }
    finally {
        $ErrorActionPreference = $prevEa
        if ($null -eq $prevAsk) {
            Remove-Item Env:\SSH_ASKPASS -ErrorAction SilentlyContinue
        }
        else {
            $env:SSH_ASKPASS = $prevAsk
        }
        if ($null -eq $prevReq) {
            Remove-Item Env:\SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue
        }
        else {
            $env:SSH_ASKPASS_REQUIRE = $prevReq
        }
        if ($null -eq $prevDisp) {
            Remove-Item Env:\DISPLAY -ErrorAction SilentlyContinue
        }
        else {
            $env:DISPLAY = $prevDisp
        }
    }
}

function Invoke-Remote {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$RemoteCommand,
        [string]$AskCmd
    )
    $argsList = @($SshBaseArgs) + @($SshTarget, $RemoteCommand)
    Invoke-SshNative -Binary ssh -Arguments $argsList -AskCmd $AskCmd -FailMessage ('ssh failed: ' + $RemoteCommand)
}

function Send-File {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$LocalPath,
        [string]$RemoteFile,
        [string]$AskCmd,
        [int]$Tries = 3
    )
    $remoteSpec = $SshTarget + ':' + $RemoteFile
    $argsList = @($SshBaseArgs) + @($LocalPath, $remoteSpec)
    $lastErr = $null
    for ($i = 1; $i -le $Tries; $i++) {
        try {
            Invoke-SshNative -Binary scp -Arguments $argsList -AskCmd $AskCmd -FailMessage 'scp failed'
            return
        }
        catch {
            $lastErr = $_
            Write-Host ('scp try ' + $i + '/' + $Tries + ' failed: ' + $_.Exception.Message)
            if ($i -lt $Tries) {
                Start-Sleep -Seconds (3 * $i)
            }
        }
    }
    throw $lastErr
}

function Receive-File {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$RemoteFile,
        [string]$LocalPath,
        [string]$AskCmd,
        [int]$Tries = 3
    )
    $remoteSpec = $SshTarget + ':' + $RemoteFile
    $argsList = @($SshBaseArgs) + @($remoteSpec, $LocalPath)
    $lastErr = $null
    for ($i = 1; $i -le $Tries; $i++) {
        try {
            Invoke-SshNative -Binary scp -Arguments $argsList -AskCmd $AskCmd -FailMessage 'scp download failed'
            return
        }
        catch {
            $lastErr = $_
            Write-Host ('scp download try ' + $i + '/' + $Tries + ' failed: ' + $_.Exception.Message)
            if ($i -lt $Tries) {
                Start-Sleep -Seconds (3 * $i)
            }
        }
    }
    throw $lastErr
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

if ($PSCmdlet.ParameterSetName -eq 'Copy') {
    $destArcade = Resolve-CopyArcadeDestination -Path $Copy
    Copy-ArcadeTree -SourceRoot $RepoRoot -DestArcade $destArcade
    exit 0
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

if ([string]::IsNullOrEmpty($Auth)) {
    throw 'SSH password required. Use -Auth, machines.json password, or ARCADE_SSH_PASSWORD.'
}

$sshTarget = $SshUser + '@' + $cabinetHost
# После смены NVMe Dropbear выдаёт новые host keys — сбрасываем known_hosts.
Write-Host ('... clearing SSH host keys for ' + $cabinetHost)
$prevEa = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & ssh-keygen -R $cabinetHost 1>$null 2>$null
    & ssh-keygen -R ('[' + $cabinetHost + ']:22') 1>$null 2>$null
    if ($Target -ne $cabinetHost) {
        & ssh-keygen -R $Target 1>$null 2>$null
    }
}
finally {
    $ErrorActionPreference = $prevEa
}

$sshBaseArgs = @(
    '-o', ('HostName=' + $cabinetHost),
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=NUL',
    '-o', 'GlobalKnownHostsFile=NUL',
    '-o', 'UpdateHostKeys=no',
    '-o', 'PreferredAuthentications=password',
    '-o', 'PubkeyAuthentication=no',
    '-o', 'Ciphers=aes128-ctr,aes256-ctr',
    '-o', 'MACs=hmac-sha2-256,hmac-sha1',
    '-o', 'ConnectTimeout=15',
    '-o', 'LogLevel=ERROR'
)

Write-Host ('-> target ' + $sshTarget)
Write-Host ('-> remote ' + $RemotePath)
Write-Host ('-> repo   ' + $RepoRoot)
if ($Update) {
    if ($NoDeploy) {
        throw '-Update cannot be combined with -NoDeploy (Update always runs on-device deploy).'
    }
    $Restart = $true
    Write-Host '-> Update: sync new/changed + missing files, deploy runtime, restart services'
}
Write-Host '... password auth'

$askCmd = New-AskPassScript -Text $Auth

if ($PSCmdlet.ParameterSetName -eq 'PullVenv') {
    $remoteTmp = '/tmp/arcade-venv-' + $PID + '.tar.gz'
    $localVendor = Join-Path $RepoRoot 'vendor\venv'
    New-Item -ItemType Directory -Path $localVendor -Force | Out-Null
    $localTmp = Join-Path $env:TEMP ('arcade-venv-' + $PID + '.tar.gz')
    Write-Host '... packing remote scripts/.venv (vendor-venv)'
    $packSh = @"
set -e
VENV=/userdata/system/scripts/.venv
PY="`$VENV/bin/python"
[ -x "`$PY" ] || { echo "ERROR: missing `$PY — boot Arcade main once first"; exit 1; }
SZ=`$(wc -c < "`$PY" 2>/dev/null || echo 0)
[ "`$SZ" -ge 64 ] || { echo "ERROR: broken venv python size=`$SZ"; exit 1; }
`$PY -c "import luma.led_matrix" || { echo "ERROR: luma not installed in venv"; exit 1; }
ARCH=`$(uname -m)
case "`$ARCH" in aarch64|arm64) NAME=aarch64.tar.gz ;; x86_64|amd64) NAME=x86_64.tar.gz ;; *) NAME=`$ARCH.tar.gz ;; esac
echo "ARCH_NAME=`$NAME"
cd /userdata/system/scripts
tar -czf '$remoteTmp' .venv
ls -la '$remoteTmp'
if [ -d /userdata/system/Arcade ]; then
  mkdir -p /userdata/system/Arcade/vendor/venv
  cp -f '$remoteTmp' "/userdata/system/Arcade/vendor/venv/`$NAME"
  echo "seeded Arcade/vendor/venv/`$NAME"
fi
"@
    $packSh = ($packSh -replace "`r`n", "`n" -replace "`r", "`n")
    if (-not $packSh.EndsWith("`n")) { $packSh += "`n" }
    $packB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($packSh))
    $packCmd = "echo $packB64 | base64 -d | sh"
    $prevAsk = $null
    $prevReq = $null
    $prevDisp = $null
    try {
        $prevAsk = $env:SSH_ASKPASS
        $prevReq = $env:SSH_ASKPASS_REQUIRE
        $prevDisp = $env:DISPLAY
        $env:SSH_ASKPASS = $askCmd
        $env:SSH_ASKPASS_REQUIRE = 'force'
        $env:DISPLAY = '1'
        $prevEa = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $packOut = & ssh @sshBaseArgs $sshTarget $packCmd 2>&1
        $packCode = $LASTEXITCODE
        $ErrorActionPreference = $prevEa
        $packText = ($packOut | ForEach-Object { "$_" }) -join "`n"
        Write-Host $packText.TrimEnd()
        if ($packCode -ne 0) {
            throw ('remote venv pack failed (exit ' + $packCode + ')')
        }
        $archName = 'aarch64.tar.gz'
        if ($packText -match 'ARCH_NAME=(\S+)') {
            $archName = $Matches[1].Trim()
        }
        $localDest = Join-Path $localVendor $archName
        Write-Host ('... downloading -> ' + $localDest)
        Receive-File -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -RemoteFile $remoteTmp -LocalPath $localTmp -AskCmd $askCmd
        Move-Item -LiteralPath $localTmp -Destination $localDest -Force
        $mb = [math]::Round((Get-Item -LiteralPath $localDest).Length / 1MB, 2)
        Write-Host ('OK vendor venv: ' + $localDest + ' (' + $mb + ' MB)')
        Write-Host 'Next: .\deploy\deploy.ps1 -Copy C:\Temp\Batocera\system'
        Write-Host '  (will seed system\scripts\.venv from this archive)'
    }
    finally {
        if ($null -eq $prevAsk) { Remove-Item Env:\SSH_ASKPASS -ErrorAction SilentlyContinue } else { $env:SSH_ASKPASS = $prevAsk }
        if ($null -eq $prevReq) { Remove-Item Env:\SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue } else { $env:SSH_ASKPASS_REQUIRE = $prevReq }
        if ($null -eq $prevDisp) { Remove-Item Env:\DISPLAY -ErrorAction SilentlyContinue } else { $env:DISPLAY = $prevDisp }
        try {
            Invoke-Remote -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -RemoteCommand ("rm -f '" + $remoteTmp + "'") -AskCmd $askCmd
        }
        catch { }
        Remove-Item -LiteralPath $askCmd -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $localTmp -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

$tarPath = Join-Path $env:TEMP ('arcade-sync-' + $PID + '.tar')
$remoteTar = '/tmp/arcade-sync-' + $PID + '.tar'

$excludeArgs = Get-DeployExcludeArgs -IncludeFull:$Full

if ($Full) {
    Write-Host 'NOTE: -Full ships wheels/vendor (heavy NVMe write). Prefer lite deploy for day-to-day.'
}

$syncedOk = $false
try {
    Write-Host ('... packing (Full=' + $Full + ')...')
    # ustar: safer across Windows OpenSSH tar ↔ Batocera GNU/BusyBox tar
    & tar --format=ustar -cf $tarPath @excludeArgs -C $RepoRoot .
    if ($LASTEXITCODE -ne 0) {
        throw ('tar create failed (exit ' + $LASTEXITCODE + ')')
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $tarPath).Length / 1MB, 2)
    Write-Host ('... uploading ' + $sizeMb + ' MB...')
    Send-File -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -LocalPath $tarPath -RemoteFile $remoteTar -AskCmd $askCmd

    # Safe remote path:
    # 1) preflight userdata I/O (abort before any live wipe)
    # 2) stop services (avoid open-file → 0-byte races)
    # 3) extract to /tmp staging, verify
    # 4) promote to Arcade.new on userdata, verify, atomic swap
    # 5) main.py deploy (no prior runtime wipe) + verify runtime sizes
    # Never delete live runtime before staging+checkout verify succeeds.
    $remoteSh = @"
set -e
RP='$RemotePath'
TAR='$remoteTar'
STAGE="/tmp/arcade-stage-`$`$"
NEW="`$RP.new"
OLD="`$RP.old"
PROBE="/userdata/system/.arcade-io-probe"

die() {
  echo "ERROR: `$*" >&2
  echo "ERROR: live Arcade/runtime was NOT wiped before this failure." >&2
  rm -rf "`$STAGE" "`$NEW" 2>/dev/null || true
  exit 1
}

echo '=== preflight userdata I/O ==='
mkdir -p /userdata/system || die "cannot mkdir /userdata/system (disk I/O?). Fix NVMe/power; aborting."
printf 'ok\n' > "`$PROBE" || die "userdata write failed (I/O). Fix NVMe/power; aborting."
sync || true
PSZ=`$(wc -c < "`$PROBE" 2>/dev/null || echo 0)
rm -f "`$PROBE"
[ "`$PSZ" -ge 2 ] || die "userdata probe readback failed (I/O)."

echo '=== stop arcade services ==='
for s in main timer server tvon; do batocera-services stop "`$s" 2>/dev/null || true; done
killall -q cec-client 2>/dev/null || true
pkill -f '/userdata/system/scripts/.*/(main|timer|server|tvon)\.py' 2>/dev/null || true
pkill -f '/userdata/system/Arcade/scripts/.*/(main|timer|server|tvon)\.py' 2>/dev/null || true
sleep 1

echo '=== extract to staging (tmp) ==='
rm -rf "`$STAGE"
mkdir -p "`$STAGE" || die "cannot mkdir staging"
if tar --help 2>&1 | grep -q overwrite; then
  tar --overwrite -xf "`$TAR" -C "`$STAGE" || die "tar extract to staging failed"
else
  tar -xf "`$TAR" -C "`$STAGE" || die "tar extract to staging failed"
fi
rm -f "`$TAR"

echo '=== verify staging ==='
[ -f "`$STAGE/scripts/tvon/tvon.py" ] || die "staging missing tvon.py"
[ -f "`$STAGE/scripts/main/main.py" ] || die "staging missing main.py"
SZ=`$(wc -c < "`$STAGE/scripts/tvon/tvon.py")
SZ2=`$(wc -c < "`$STAGE/scripts/main/main.py")
echo "staging tvon.py=`$SZ main.py=`$SZ2"
[ "`$SZ" -ge 10000 ] && [ "`$SZ2" -ge 10000 ] || die "staging extract empty/tiny (tvon=`$SZ main=`$SZ2)"

echo '=== promote checkout (Arcade.new -> swap) ==='
rm -rf "`$NEW" "`$OLD"
mkdir -p "`$NEW" || die "cannot mkdir Arcade.new (userdata I/O?)"
# copy verified tree onto userdata without touching live RP yet
if command -v rsync >/dev/null 2>&1; then
  rsync -a "`$STAGE"/ "`$NEW"/ || die "rsync to Arcade.new failed"
else
  cp -a "`$STAGE"/. "`$NEW"/ || die "cp to Arcade.new failed"
fi
sync || true
NSZ=`$(wc -c < "`$NEW/scripts/tvon/tvon.py")
NSZ2=`$(wc -c < "`$NEW/scripts/main/main.py")
echo "Arcade.new tvon.py=`$NSZ main.py=`$NSZ2"
[ "`$NSZ" -ge 10000 ] && [ "`$NSZ2" -ge 10000 ] || die "Arcade.new empty/tiny after copy (tvon=`$NSZ main=`$NSZ2)"

# atomic-ish swap; keep OLD until NEW is in place
if [ -e "`$RP" ]; then
  mv "`$RP" "`$OLD" || die "cannot move live Arcade aside"
fi
mv "`$NEW" "`$RP" || {
  echo "ERROR: swap Arcade.new -> Arcade failed; restoring OLD if present" >&2
  if [ -e "`$OLD" ]; then mv "`$OLD" "`$RP" || true; fi
  die "checkout swap failed"
}
rm -rf "`$OLD" "`$STAGE"
sync || true
echo "checkout ok: `$RP"

find "`$RP" -type f -size 0 \( -name '*.py' -o -name '*.toml' \) -delete 2>/dev/null || true

echo '=== Pi5 NVMe cmdline (ASPM off) ==='
mount -o remount,rw /boot 2>/dev/null || true
if [ -f /boot/cmdline.txt ]; then
  BEFORE=`$(tr -d '\r\n' < /boot/cmdline.txt)
  AFTER="`$BEFORE"
  for tok in nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off; do
    case " `$AFTER " in
      *" `$tok "*) ;;
      *) AFTER="`$AFTER `$tok" ;;
    esac
  done
  if [ "`$AFTER" = "`$BEFORE" ]; then
    echo CMDLINE_ALREADY_OK
  else
    cp -a /boot/cmdline.txt /boot/cmdline.txt.bak.nvme 2>/dev/null || true
    printf '%s\n' "`$AFTER" > /boot/cmdline.txt
    sync || true
    echo CMDLINE_PATCHED
  fi
  echo "`$AFTER"
else
  echo NO_CMDLINE
fi
mount -o remount,ro /boot 2>/dev/null || true
"@
    if (-not $NoDeploy) {
        $remoteSh = $remoteSh.TrimEnd() + @"

echo '=== on-device deploy (no prior runtime wipe) ==='
python3 '$RemotePath/scripts/main/main.py' deploy || die "main.py deploy failed"
sync || true
echo '=== verify runtime sizes (after sync) ==='
RSZ=`$(wc -c < /userdata/system/scripts/tvon/tvon.py 2>/dev/null || echo 0)
RSZ2=`$(wc -c < /userdata/system/scripts/main/main.py 2>/dev/null || echo 0)
echo "runtime tvon.py=`$RSZ main.py=`$RSZ2"
[ "`$RSZ" -ge 10000 ] && [ "`$RSZ2" -ge 10000 ] || die "runtime scripts empty/tiny after deploy (tvon=`$RSZ main=`$RSZ2)"
"@
    }
    if ($Restart) {
        $remoteSh = $remoteSh.TrimEnd() + @"

echo '=== restart services ==='
# main service heals 0-byte runtime from Arcade on start if needed
batocera-services restart main timer server tvon || true
"@
    }
    $remoteSh = ($remoteSh -replace "`r`n", "`n" -replace "`r", "`n")
    if (-not $remoteSh.EndsWith("`n")) {
        $remoteSh += "`n"
    }
    $remoteB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteSh))
    $remoteCmd = "echo $remoteB64 | base64 -d | sh"

    $extractMsg = 'extracting'
    if (-not $NoDeploy) {
        $extractMsg = $extractMsg + ' + deploy'
    }
    if ($Restart) {
        $extractMsg = $extractMsg + ' + restart'
    }
    if ($Update) {
        $extractMsg = 'Update: ' + $extractMsg
    }
    Write-Host ('... ' + $extractMsg)
    Invoke-Remote -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -RemoteCommand $remoteCmd -AskCmd $askCmd

    Write-Host ('OK synced to ' + $cabinetHost)
    if ($Update) {
        Write-Host '  Updated Arcade checkout + runtime; services restarted (main timer server tvon).'
    }
    elseif (-not $NoDeploy) {
        Write-Host ('  Next: Cursor Remote-SSH -> ' + $sshTarget + ' -> open ' + $RemotePath + ' -> F5 Arcade')
    }
    $syncedOk = $true
}
finally {
    Remove-Item -LiteralPath $tarPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $askCmd -Force -ErrorAction SilentlyContinue
}

if ($syncedOk -and -not $NoLogs) {
    Write-Host ''
    Write-Host '... attaching logs (Ctrl+C to stop)'
    $logsScript = Join-Path $PSScriptRoot 'logs.ps1'
    & $logsScript -Target $Target -SshUser $SshUser -Auth $Auth
}
