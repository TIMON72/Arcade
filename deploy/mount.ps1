# Mount a Batocera SSD/USB (PHYSICALDRIVEn) into WSL and open it in Explorer.
# Usage:
#   .\deploy\mount.ps1 -Mount            # list disks, ask number, mount
#   .\deploy\mount.ps1 3
#   .\deploy\mount.ps1 3 -Boot
#   .\deploy\mount.ps1 -List
#   .\deploy\mount.ps1 -Mount            # list disks, ask, mount + Explorer perms (chown)
#   .\deploy\mount.ps1 -FixPerms         # again after Explorer paste (new files / services +x)
#   .\deploy\mount.ps1 3 -Unmount
#   .\deploy\mount.ps1 -Unmount

[CmdletBinding(DefaultParameterSetName = 'Mount')]
param(
    [Parameter(Position = 0, ParameterSetName = 'Mount')]
    [Parameter(Position = 0, ParameterSetName = 'Unmount')]
    [ValidateRange(0, 64)]
    [int]$DiskNumber = -1,

    [Parameter(Mandatory = $true, ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(Mandatory = $true, ParameterSetName = 'FixPerms')]
    [switch]$FixPerms,

    [Parameter(ParameterSetName = 'Mount')]
    [switch]$Mount,

    [Parameter(ParameterSetName = 'Mount')]
    [switch]$Boot,

    [Parameter(ParameterSetName = 'Unmount')]
    [switch]$Unmount,

    [Parameter(ParameterSetName = 'Mount')]
    [Parameter(ParameterSetName = 'Unmount')]
    [Parameter(ParameterSetName = 'FixPerms')]
    [string]$Distro,

    # Internal: already elevated child
    [Parameter(ParameterSetName = 'Mount')]
    [Parameter(ParameterSetName = 'Unmount')]
    [switch]$Elevated
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ShareMount = '/mnt/wsl/batocera-share'
$BootMount = '/mnt/wsl/batocera-boot'
$LogPath = Join-Path $env:TEMP 'arcade-mount.log'

function Write-MountLog {
    param([string]$Message)
    $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Host $Message
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = [Security.Principal.WindowsPrincipal]::new($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-DefaultWslDistro {
    if ($Distro) { return $Distro }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $raw = & wsl -l -q 2>&1 | Out-String
    $ErrorActionPreference = $prev
    $text = ($raw -replace "`0", '').Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($text)) {
        throw "WSL not available / no distro. Install Ubuntu from Microsoft Store."
    }
    $first = ($text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if (-not $first) {
        throw "No WSL distro found. Install Ubuntu from Microsoft Store."
    }
    return $first.Trim()
}

function Invoke-WslRoot {
    param([string]$Bash)
    $d = $script:DistroName
    $tmp = Join-Path $env:TEMP ("batocera-mount-{0}.sh" -f [guid]::NewGuid().ToString('N'))
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($tmp, $Bash.Replace("`r`n", "`n").Replace("`r", "`n"), $utf8NoBom)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $wslPath = & wsl -d $d -e wslpath -a $tmp 2>&1
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($wslPath | Out-String))) {
            throw "wslpath failed for $tmp : $wslPath"
        }
        $wslPath = (($wslPath | Out-String) -replace "`0", '').Trim()
        $out = & wsl -d $d -u root -e bash $wslPath 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        $text = foreach ($line in @($out)) {
            if ($line -is [System.Management.Automation.ErrorRecord]) { $line.ToString() } else { "$line" }
        }
        return [pscustomobject]@{
            ExitCode = $code
            Output   = (($text -join "`n") -replace "`0", '').TrimEnd()
        }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Update-BatoceraNvmeCmdlineViaWsl {
    param([string]$BootMountPath = $BootMount)
    # Pi5+NVMe: disable ASPM / NVMe deep power-save in cmdline.txt
    $bash = @"
set -e
BOOT='$BootMountPath'
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
    return (Invoke-WslRoot -Bash $bash)
}

function Show-Disks {
    Write-Host ''
    Write-Host 'Windows disks:'
    Get-Disk | Sort-Object Number | ForEach-Object {
        $gb = [math]::Round($_.Size / 1GB, 1)
        $mark = if ($_.Number -eq 0) { ' (usually system - skip)' } else { '' }
        Write-Host ("  {0,2}  {1,-28} {2,7} GB  {3,-6}  {4}{5}" -f `
            $_.Number, $_.FriendlyName, $gb, $_.PartitionStyle, $_.OperationalStatus, $mark)
    }
    Write-Host ''
    Write-Host 'Tip: Batocera userdata is often ~entire disk minus ~6 GB boot; Windows may show RAW/MBR.'
}

function Request-Elevation {
    param([string[]]$ScriptArgs)
    if (Test-IsAdmin) { return $false }

    Remove-Item -LiteralPath $LogPath -Force -ErrorAction SilentlyContinue
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # One string: PS 5.1 Start-Process ArgumentList array quoting is unreliable
    $joined = ($ScriptArgs | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated $joined"

    Write-Host 'Requesting Administrator (needed for wsl --mount)...'
    Write-Host "Log: $LogPath"
    $p = Start-Process -FilePath $ps -Verb RunAs -PassThru -Wait -ArgumentList $argLine
    if (Test-Path -LiteralPath $LogPath) {
        Write-Host '---- elevated log ----'
        Get-Content -LiteralPath $LogPath -Encoding UTF8 | Write-Host
        Write-Host '---- end ----'
    }
    if ($null -eq $p -or $p.ExitCode -ne 0) {
        $code = if ($null -eq $p) { 1 } else { $p.ExitCode }
        throw "Elevated mount failed (exit $code). See log above / $LogPath"
    }
    return $true
}

function Dismount-BatoceraMounts {
    $null = Invoke-WslRoot @'
# lazy umount — plain umount can hang forever if Explorer holds the share
umount -l /mnt/wsl/batocera-share 2>/dev/null || true
umount -l /mnt/wsl/batocera-boot 2>/dev/null || true
rmdir /mnt/wsl/batocera-share /mnt/wsl/batocera-boot 2>/dev/null || true
'@
}

function Unmount-PhysicalDisk {
    param([int]$Number)
    Dismount-BatoceraMounts
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    if ($Number -ge 0) {
        $path = "\\.\PHYSICALDRIVE$Number"
        Write-MountLog "Unmounting $path ..."
        & wsl --unmount $path 2>&1 | ForEach-Object { Write-MountLog "$_" }
    }
    Write-MountLog 'wsl --unmount (all attached)...'
    & wsl --unmount 2>&1 | ForEach-Object { Write-MountLog "$_" }
    $ErrorActionPreference = $prev
}

function Clear-DiskDriveLetters {
    param([int]$Number)
    Write-MountLog "Releasing Windows mounts on disk $Number (needed for wsl --mount)..."
    $parts = @(Get-Partition -DiskNumber $Number -ErrorAction SilentlyContinue)
    foreach ($p in $parts) {
        if ($p.DriveLetter) {
            $letter = "$($p.DriveLetter):"
            Write-MountLog "  remove drive letter $letter (partition $($p.PartitionNumber))"
            try {
                Remove-PartitionAccessPath -DiskNumber $Number -PartitionNumber $p.PartitionNumber -AccessPath ($letter + '\') -ErrorAction Stop
            } catch {
                Write-MountLog "  WARN: could not remove ${letter}: $($_.Exception.Message)"
            }
        }

        $volPath = $null
        try {
            $vol = Get-Volume -Partition $p -ErrorAction SilentlyContinue
            if ($null -ne $vol -and $vol.Path) {
                $volPath = [string]$vol.Path
            }
        } catch {
            $volPath = $null
        }
        if ($volPath) {
            # Dismount without letter: mountvol /P (Storage's Dismount-Volume may be absent)
            Write-MountLog "  mountvol /P $volPath"
            $prev = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $mv = & mountvol.exe $volPath /P 2>&1 | Out-String
            $ErrorActionPreference = $prev
            if ($mv.Trim()) { Write-MountLog ('  ' + $mv.Trim()) }
        }
    }

    try {
        Write-MountLog "  offline/online disk $Number"
        Set-Disk -Number $Number -IsOffline $true -ErrorAction Stop
        Start-Sleep -Seconds 1
        Set-Disk -Number $Number -IsOffline $false -ErrorAction Stop
        Start-Sleep -Seconds 1
    } catch {
        Write-MountLog "  WARN: offline/online failed: $($_.Exception.Message)"
    }
}

function Mount-PhysicalDiskBare {
    param([int]$Number)
    $path = "\\.\PHYSICALDRIVE$Number"
    Clear-DiskDriveLetters -Number $Number
    Write-MountLog "Attaching $path to WSL (bare)..."
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & wsl --mount $path --bare 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    $out = ($out -replace "`0", '').Trim()
    # Decode common Win32 HRESULT in WSL mount errors
    if ($out -match '0x8007006c' -or $code -ne 0) {
        $outReadable = $out
        if ($out -match '0x8007006c') {
            $outReadable = $out + "`nHINT: disk locked (ERROR_DRIVE_LOCKED). Close Explorer windows for that disk, remove drive letters (e.g. E:), then retry."
        }
        if ($out) { Write-MountLog $outReadable }
    } elseif ($out) {
        Write-MountLog $out
    }
    if ($code -ne 0) {
        if ($out -notmatch 'already|уже|ALREADY') {
            throw "wsl --mount failed (exit $code): $out"
        }
        Write-MountLog 'Disk already attached.'
    }
}

function Mount-BatoceraPartitions {
    $bash = @'
set -e
mkdir -p /mnt/wsl/batocera-share /mnt/wsl/batocera-boot
umount /mnt/wsl/batocera-share 2>/dev/null || true
umount /mnt/wsl/batocera-boot 2>/dev/null || true

echo '=== block devices ==='
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT

SHARE_DEV=$(blkid -L SHARE 2>/dev/null || true)
BOOT_DEV=$(blkid -L BATOCERA 2>/dev/null || true)

if [ -z "$SHARE_DEV" ]; then
  SHARE_DEV=$(lsblk -pn -o NAME,FSTYPE,LABEL | awk '$2=="ext4" && $3=="SHARE" {print $1; exit}')
fi
if [ -z "$BOOT_DEV" ]; then
  BOOT_DEV=$(lsblk -pn -o NAME,FSTYPE,LABEL | awk '$2=="vfat" && $3=="BATOCERA" {print $1; exit}')
fi

if [ -z "$SHARE_DEV" ] || [ -z "$BOOT_DEV" ]; then
  for disk in /dev/sd[a-z]; do
    [ -b "$disk" ] || continue
    parts=$(lsblk -pn -o NAME,FSTYPE "$disk" | tail -n +2)
    echo "$parts" | grep -q vfat || continue
    echo "$parts" | grep -q ext4 || continue
    if [ -z "$BOOT_DEV" ]; then
      BOOT_DEV=$(echo "$parts" | awk '$2=="vfat" {print $1; exit}')
    fi
    if [ -z "$SHARE_DEV" ]; then
      SHARE_DEV=$(lsblk -pnb -o NAME,FSTYPE,SIZE "$disk" | awk '$2=="ext4" {print $3, $1}' | sort -nr | head -1 | awk '{print $2}')
    fi
  done
fi

echo "BOOT_DEV=$BOOT_DEV"
echo "SHARE_DEV=$SHARE_DEV"

if [ -z "$SHARE_DEV" ]; then
  echo 'ERROR: SHARE/ext4 partition not found on attached disks.' >&2
  exit 2
fi

mount -o rw "$SHARE_DEV" /mnt/wsl/batocera-share
if [ -n "$BOOT_DEV" ]; then
  mount -o rw "$BOOT_DEV" /mnt/wsl/batocera-boot || true
fi

# Explorer write: ONLY top-level dirs (never chmod -R — roms/ can hang for minutes)
echo '=== permissions for Explorer write (top-level only) ==='
chmod a+rwx /mnt/wsl/batocera-share || true
chmod a+rwx /mnt/wsl/batocera-share/* 2>/dev/null || true
chmod 700 /mnt/wsl/batocera-share/lost+found 2>/dev/null || true
if mountpoint -q /mnt/wsl/batocera-boot; then
  mount -o remount,rw,umask=000 /mnt/wsl/batocera-boot 2>/dev/null || true
fi

echo '=== mounted ==='
df -h /mnt/wsl/batocera-share 2>/dev/null || true
df -h /mnt/wsl/batocera-boot 2>/dev/null || true
echo '=== SHARE top ==='
ls -la /mnt/wsl/batocera-share | head -30
'@
    $r = Invoke-WslRoot -Bash $bash
    if ($r.Output) { Write-MountLog $r.Output }
    if ($r.ExitCode -ne 0) {
        throw "Failed to mount Batocera partitions (exit $($r.ExitCode))."
    }
}

function Open-ExplorerMounts {
    param([switch]$AlsoBoot)
    $uncShare = "\\wsl.localhost\$($script:DistroName)\mnt\wsl\batocera-share"
    $uncBoot = "\\wsl.localhost\$($script:DistroName)\mnt\wsl\batocera-boot"
    Write-MountLog "Explorer SHARE: $uncShare"
    Start-Process explorer.exe $uncShare
    if ($AlsoBoot) {
        Write-MountLog "Explorer BOOT:  $uncBoot"
        Start-Process explorer.exe $uncBoot
    }
}

function Read-DiskNumberToMount {
    Show-Disks
    Write-Host "WSL distro: $($script:DistroName)"
    Write-Host 'Enter disk number to mount (or blank to cancel):'
    $raw = Read-Host 'DiskNumber'
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw 'Cancelled.'
    }
    $n = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$n)) {
        throw "Not a number: $raw"
    }
    if ($n -lt 0 -or $n -gt 64) {
        throw "Disk number out of range: $n"
    }
    $null = Get-Disk -Number $n -ErrorAction Stop
    return $n
}

function Repair-SharePermissions {
    Write-Host 'Fixing SHARE for Explorer (chown to WSL user — needed to delete/copy in Explorer)...'
    $r = Invoke-WslRoot @'
set -e
SHARE=/mnt/wsl/batocera-share
if ! mountpoint -q "$SHARE"; then
  echo "ERROR: $SHARE is not mounted. Run .\deploy\mount.ps1 -Mount first." >&2
  exit 2
fi

# \\wsl.localhost Explorer maps to the default WSL user, NOT root.
# root:root trees often refuse delete/rename from Explorer even with 777.
USER_NAME="$(getent passwd 1000 2>/dev/null | cut -d: -f1 || true)"
if [ -z "$USER_NAME" ]; then
  USER_NAME="$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')"
fi
if [ -z "$USER_NAME" ]; then
  USER_NAME="$(ls -1 /home 2>/dev/null | head -1)"
fi
if [ -z "$USER_NAME" ]; then
  echo "ERROR: cannot detect WSL username for chown" >&2
  exit 3
fi
echo "WSL user for Explorer: $USER_NAME"

chmod a+rwx "$SHARE" || true
# Keep lost+found root-only
if [ -d "$SHARE/lost+found" ]; then
  chown root:root "$SHARE/lost+found" || true
  chmod 700 "$SHARE/lost+found" || true
fi

# chown each top-level entry (incl. roms/system) so Explorer can delete
for p in "$SHARE"/*; do
  [ -e "$p" ] || continue
  base="$(basename "$p")"
  [ "$base" = "lost+found" ] && continue
  echo "  chown -R $USER_NAME:$USER_NAME $base"
  chown -R "$USER_NAME:$USER_NAME" "$p" || true
  chmod -R a+rwX "$p" || true
done

if [ -d "$SHARE/system/services" ]; then
  chmod a+rx "$SHARE/system/services" || true
  for f in "$SHARE/system/services"/*; do
    [ -f "$f" ] || continue
    chmod a+rx "$f"
    sed -i 's/\r$//' "$f" 2>/dev/null || true
    echo "  +x $(basename "$f")"
  done
else
  echo "NOTE: no system/services yet"
fi

echo "=== top ==="
ls -la "$SHARE" | head -25
echo OK
'@
    if ($r.Output) { Write-Host $r.Output }
    if ($r.ExitCode -ne 0) {
        throw "FixPerms failed (exit $($r.ExitCode)). Is SHARE mounted?"
    }
}

function Wait-IfElevatedWindow {
    param([int]$Code = 0)
    if (-not $Elevated) { return }
    Write-Host ''
    if ($Code -ne 0) {
        Write-Host "FAILED (exit $Code). Log: $LogPath" -ForegroundColor Red
        Write-Host 'Press Enter to close this window...'
        try { [void][Console]::ReadLine() } catch { Start-Sleep -Seconds 12 }
    } else {
        Write-Host "OK. Closing..." -ForegroundColor Green
        Start-Sleep -Seconds 2
    }
}

# --- main ---
$exitCode = 0
try {
    if ($Elevated) {
        Remove-Item -LiteralPath $LogPath -Force -ErrorAction SilentlyContinue
        Write-MountLog "Elevated host: $([Environment]::UserName) admin=$(Test-IsAdmin)"
    }

    $script:DistroName = Get-DefaultWslDistro

    if ($List -or ($PSCmdlet.ParameterSetName -eq 'List')) {
        Show-Disks
        Write-Host "WSL distro: $($script:DistroName)"
        $st = Invoke-WslRoot "lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT; echo; mount | grep -E 'batocera|SHARE|BATOCERA' || true"
        Write-Host $st.Output
        exit 0
    }

    if ($FixPerms -or ($PSCmdlet.ParameterSetName -eq 'FixPerms')) {
        Repair-SharePermissions
        exit 0
    }

    $doUnmount = ($PSCmdlet.ParameterSetName -eq 'Unmount') -or $Unmount
    if ($doUnmount) {
        $elevateArgs = @('-Unmount')
        if ($DiskNumber -ge 0) { $elevateArgs = @("$DiskNumber", '-Unmount') }
        if ($Distro) { $elevateArgs += @('-Distro', $Distro) }

        if (-not (Test-IsAdmin)) {
            $null = Request-Elevation -ScriptArgs $elevateArgs
            # Explorer not needed; child did the work
            if (-not $Elevated) { exit 0 }
        }

        Unmount-PhysicalDisk -Number $DiskNumber
        Write-MountLog 'Done (unmount).'
        Wait-IfElevatedWindow -Code 0
        exit 0
    }

    if ($DiskNumber -lt 0) {
        if (-not $Mount) {
            throw "Disk number required. Example: .\deploy\mount.ps1 -Mount   or   .\deploy\mount.ps1 3"
        }
        $DiskNumber = Read-DiskNumberToMount
    }

    $elevateArgs = @("$DiskNumber")
    if ($Boot) { $elevateArgs += '-Boot' }
    if ($Distro) { $elevateArgs += @('-Distro', $Distro) }

    $handedOff = $false
    if (-not (Test-IsAdmin)) {
        $handedOff = Request-Elevation -ScriptArgs $elevateArgs
    }

    if ($handedOff -and -not $Elevated) {
        # Child already mounted + FixPerms; open Explorer in THIS (user) session
        Open-ExplorerMounts -AlsoBoot:$Boot
        Write-Host ''
        Write-Host 'Explorer write/delete is enabled on existing SHARE files.'
        Write-Host 'After pasting new system\ from Windows, run once more:'
        Write-Host '  .\deploy\mount.ps1 -FixPerms'
        Write-Host 'When finished, before unplugging:'
        Write-Host "  .\deploy\mount.ps1 $DiskNumber -Unmount"
        exit 0
    }

    # Admin path (elevated child or already admin)
    $disk = Get-Disk -Number $DiskNumber -ErrorAction Stop
    $gb = [math]::Round($disk.Size / 1GB, 1)
    Write-MountLog ("Disk {0}: {1} ({2} GB, {3})" -f $disk.Number, $disk.FriendlyName, $gb, $disk.PartitionStyle)
    Write-MountLog "WSL distro: $($script:DistroName)"

    Dismount-BatoceraMounts
    Write-MountLog 'Detaching any previously mounted physical disks from WSL...'
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & wsl --unmount 2>&1 | Out-Null
    $ErrorActionPreference = $prev

    Mount-PhysicalDiskBare -Number $DiskNumber
    Start-Sleep -Seconds 1
    Mount-BatoceraPartitions
    # Immediately: Explorer must be able to write/delete (chown away from root)
    Repair-SharePermissions

    # Pi5+NVMe: ensure ASPM/power-save off in /boot/cmdline.txt (persists across boots)
    try {
        $nvme = Update-BatoceraNvmeCmdlineViaWsl -BootMountPath $BootMount
        Write-MountLog ($nvme.Output -replace "`n", ' | ')
        if ($nvme.ExitCode -ne 0 -and $nvme.ExitCode -ne 3) {
            Write-MountLog ("WARN: cmdline NVMe patch exit " + $nvme.ExitCode)
        }
    } catch {
        Write-MountLog ("WARN: cmdline NVMe patch failed: " + $_.Exception.Message)
    }

    if (-not $Elevated) {
        Open-ExplorerMounts -AlsoBoot:$Boot
        Write-Host ''
        Write-Host 'Explorer write/delete is enabled on existing SHARE files.'
        Write-Host 'Boot cmdline NVMe tokens applied (pcie_aspm=off …) if boot was mounted.'
        Write-Host 'After pasting new system\ from Windows, run once more:'
        Write-Host '  .\deploy\mount.ps1 -FixPerms'
        Write-Host 'When finished, before unplugging:'
        Write-Host "  .\deploy\mount.ps1 $DiskNumber -Unmount"
    } else {
        Write-MountLog 'Mount OK — parent will open Explorer.'
        Write-MountLog 'Explorer perms applied. After paste: .\deploy\mount.ps1 -FixPerms'
        Write-MountLog 'Boot cmdline NVMe patch attempted.'
    }

    Write-MountLog "When finished: .\deploy\mount.ps1 $DiskNumber -Unmount"
    Wait-IfElevatedWindow -Code 0
    exit 0
}
catch {
    $exitCode = 1
    $msg = "ERROR: $($_.Exception.Message)"
    if ($Elevated) {
        Write-MountLog $msg
        if ($_.ScriptStackTrace) { Write-MountLog $_.ScriptStackTrace }
        Wait-IfElevatedWindow -Code 1
    } else {
        Write-Host $msg -ForegroundColor Red
    }
    exit $exitCode
}
