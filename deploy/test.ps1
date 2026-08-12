# Cabinet health test: reboot if online, SSH <=1m, boot <=5m, alive 5m, x3.
# Usage:
#   .\deploy\test.ps1 zero
#   .\deploy\test.ps1 zero -SshUser root -Auth linux
# After deploy/update: .\deploy\deploy.ps1 zero -Update ; .\deploy\test.ps1 zero

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,
    [string]$SshUser,
    [string]$Auth
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ArcadeMachinesConfig {
    $path = Join-Path $PSScriptRoot 'machines.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Test-ArcadeIpv4Name {
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

function Resolve-ArcadeHost {
    param(
        [string]$Name,
        $Config
    )
    if (Test-ArcadeIpv4Name -Name $Name) {
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

function New-ArcadeAskPass {
    param([string]$Text)
    $path = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.cmd')
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.ps1')
    # Без CR/LF: echo/set /p под OpenSSH ASKPASS на Windows часто ломают пароль.
    $ps = '[Console]::Out.Write(' + ("'" + $Text.Replace("'", "''") + "'") + ')'
    Set-Content -LiteralPath $askPs1 -Value $ps -Encoding ASCII
    $line1 = '@echo off'
    $line2 = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $askPs1 + '"'
    Set-Content -LiteralPath $path -Value @($line1, $line2) -Encoding ASCII
    return $path
}

function Test-TcpPortOpen {
    param(
        [string]$HostName,
        [int]$Port = 22,
        [int]$TimeoutMs = 800
    )
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $ok) {
            $client.Close()
            return $false
        }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    }
    catch {
        return $false
    }
}

function Invoke-RemoteCapture {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$RemoteCommand,
        [string]$AskCmd,
        [int]$ConnectTimeoutSec = 8
    )
    # Rebuild args with a short ConnectTimeout + keepalives (detect NVMe/SSH hangs).
    $filtered = New-Object System.Collections.Generic.List[string]
    $base = @($SshBaseArgs)
    for ($i = 0; $i -lt $base.Count; $i++) {
        $cur = [string]$base[$i]
        if ($cur -eq '-o' -and ($i + 1) -lt $base.Count) {
            $next = [string]$base[$i + 1]
            if ($next -like 'ConnectTimeout=*') {
                $i++
                continue
            }
        }
        $filtered.Add($cur) | Out-Null
    }
    $argsList = @($filtered) + @(
        '-o', ('ConnectTimeout=' + $ConnectTimeoutSec),
        '-o', 'ConnectionAttempts=1',
        '-o', 'ServerAliveInterval=2',
        '-o', 'ServerAliveCountMax=2',
        $SshTarget,
        $RemoteCommand
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
        $out = & ssh @argsList 2>&1
        $code = $LASTEXITCODE
        $text = (($out | ForEach-Object { "$_" }) -join "`n").TrimEnd()
        return [pscustomobject]@{
            Ok       = ($code -eq 0)
            ExitCode = $code
            Text     = $text
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

function Get-ArcadeTestProbeScript {
    return @'
echo ARCADE_TEST_BEGIN
FAILS=""
note_fail() { FAILS="${FAILS}|$1"; }

MAIN=/userdata/system/scripts/main/main.py
TVON=/userdata/system/scripts/tvon/tvon.py
PY=/userdata/system/scripts/.venv/bin/python
AMAIN=/userdata/system/Arcade/scripts/main/main.py
ATVON=/userdata/system/Arcade/scripts/tvon/tvon.py

SZ_MAIN=$(wc -c < "$MAIN" 2>/dev/null || echo 0)
SZ_TVON=$(wc -c < "$TVON" 2>/dev/null || echo 0)
SZ_PY=$(wc -c < "$PY" 2>/dev/null || echo 0)
SZ_AMAIN=$(wc -c < "$AMAIN" 2>/dev/null || echo 0)
SZ_ATVON=$(wc -c < "$ATVON" 2>/dev/null || echo 0)
echo "FILE_MAIN=$SZ_MAIN"
echo "FILE_TVON=$SZ_TVON"
echo "FILE_PY=$SZ_PY"
echo "FILE_ARCADE_MAIN=$SZ_AMAIN"
echo "FILE_ARCADE_TVON=$SZ_ATVON"
[ "$SZ_MAIN" -ge 10000 ] || note_fail "runtime_main_tiny"
[ "$SZ_TVON" -ge 10000 ] || note_fail "runtime_tvon_tiny"
[ "$SZ_PY" -ge 64 ] || note_fail "venv_python_broken"
[ "$SZ_AMAIN" -ge 10000 ] || note_fail "arcade_main_tiny"
[ "$SZ_ATVON" -ge 10000 ] || note_fail "arcade_tvon_tiny"

SVC=$(batocera-services list 2>/dev/null | tr '\n' ' ')
echo "SERVICES=$SVC"
echo "$SVC" | grep -q 'main;\*' || note_fail "service_main_off"
echo "$SVC" | grep -q 'tvon;\*' || note_fail "service_tvon_off"
echo "$SVC" | grep -q 'timer;\*' || note_fail "service_timer_off"
echo "$SVC" | grep -q 'server;\*' || note_fail "service_server_off"

P_MAIN=$(pgrep -f '/userdata/system/scripts/main/main.py' 2>/dev/null | head -n1)
P_TVON=$(pgrep -f '/userdata/system/scripts/tvon/tvon.py' 2>/dev/null | head -n1)
P_TIMER=$(pgrep -f '/userdata/system/scripts/timer/timer.py' 2>/dev/null | head -n1)
P_SERVER=$(pgrep -f '/userdata/system/scripts/server/server.py' 2>/dev/null | head -n1)
P_ES=$(pgrep -f '[Ee]mulation[Ss]tation' 2>/dev/null | head -n1)
echo "PID_MAIN=${P_MAIN:-0}"
echo "PID_TVON=${P_TVON:-0}"
echo "PID_TIMER=${P_TIMER:-0}"
echo "PID_SERVER=${P_SERVER:-0}"
echo "PID_ES=${P_ES:-0}"
# tvon is a boot helper: holds CEC ~active_source_keep_sec then exits.
# On a warm cabinet PID_TVON=0 is normal if HDMI+audio+ES are healthy.
[ -n "$P_MAIN" ] || note_fail "proc_main"
[ -n "$P_TIMER" ] || note_fail "proc_timer"
[ -n "$P_SERVER" ] || note_fail "proc_server"
[ -n "$P_ES" ] || note_fail "proc_emulationstation"
if [ -n "$P_TVON" ]; then
  echo "TVON_STATE=running"
else
  echo "TVON_STATE=exited_or_idle"
fi
if [ -n "$P_TIMER" ] && [ -n "$P_SERVER" ]; then
  echo "TIMER_SERVER=up"
else
  echo "TIMER_SERVER=down"
fi

HDMI=""
for n in HDMI-A-1 HDMI-A-2; do
  for p in /sys/class/drm/card*-${n}/status; do
    [ -f "$p" ] || continue
    st=$(tr -d '\r\n' < "$p" 2>/dev/null || true)
    echo "DRM_${n}=$st"
    if [ "$st" = "connected" ]; then HDMI="$n"; fi
  done
done
echo "HDMI=${HDMI:-none}"
[ -n "$HDMI" ] || note_fail "hdmi_disconnected"

# PipeWire is ground truth on RPi5 (batocera-audio get can lag / stay "auto").
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/var/run}"
AUDIO_CUR=$(batocera-audio get 2>/dev/null | tr -d '\r' | head -n1)
AUDIO_LIST=$(batocera-audio list 2>/dev/null | tr '\n' ';' | tr -d '\r')
AUDIO_PROFILE=$(batocera-settings-get audio.profile 2>/dev/null | tr -d '\r' | head -n1)
AUDIO_DEVICE=$(batocera-settings-get audio.device 2>/dev/null | tr -d '\r' | head -n1)
PACTL_DEFAULT=$(pactl get-default-sink 2>/dev/null | tr -d '\r' | head -n1)
PACTL_MUTE=$(pactl get-sink-mute @DEFAULT_SINK@ 2>/dev/null | awk '{print $2}' | tr -d '\r')
echo "AUDIO_GET=${AUDIO_CUR:-}"
echo "AUDIO_DEVICE=${AUDIO_DEVICE:-}"
echo "AUDIO_PROFILE=${AUDIO_PROFILE:-}"
echo "AUDIO_LIST=${AUDIO_LIST:-}"
echo "PACTL_DEFAULT=${PACTL_DEFAULT:-}"
echo "PACTL_MUTE=${PACTL_MUTE:-}"

# Must have a real HDMI sink listed (not only Dummy).
echo "$AUDIO_LIST" | grep -qi 'hdmi' || note_fail "no_hdmi_sink_listed"
case "${PACTL_DEFAULT}" in
  ""|*auto_null*) note_fail "pactl_default_null_or_empty" ;;
esac
echo "${PACTL_DEFAULT}" | grep -qi 'hdmi' || note_fail "pactl_default_not_hdmi"
[ "${PACTL_MUTE:-yes}" = "no" ] || note_fail "audio_muted"

# Persisted Batocera settings should target HDMI (cold-boot S27 path).
case "${AUDIO_DEVICE}" in
  ""|auto|auto_null) note_fail "audio_device_auto_or_empty" ;;
esac
echo "${AUDIO_DEVICE}" | grep -qi 'hdmi' || note_fail "audio_device_not_hdmi"
echo "${AUDIO_PROFILE}" | grep -qi 'hdmi' || note_fail "audio_profile_not_hdmi"

# TVON knobs deployed (soft-wait + hotplug suppress).
TVON_CFG=/userdata/system/scripts/tvon/config.toml
if [ -f "$TVON_CFG" ]; then
  grep -q 'audio_soft_wait_sec' "$TVON_CFG" || note_fail "tvon_cfg_missing_soft_wait"
  grep -q 'hotplug_suppress_sec' "$TVON_CFG" || note_fail "tvon_cfg_missing_hotplug_suppress"
else
  note_fail "tvon_cfg_missing"
fi

# ES: only dreamcast + nes visible (HiddenSystems).
if [ -n "$P_ES" ]; then
  VIS=$(curl -sS --max-time 3 http://127.0.0.1:1234/systems 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    print("ERR"); raise SystemExit(0)
names = []
for s in data:
    if str(s.get("visible")).lower() != "true":
        continue
    if str(s.get("collection")).lower() == "true":
        continue
    names.append(s.get("name") or "")
print(",".join(sorted(names)))
' 2>/dev/null | tr -d '\r')
  echo "ES_VISIBLE=${VIS:-}"
  case "${VIS}" in
    ""|ERR) note_fail "es_systems_api_fail" ;;
    dreamcast,nes|nes,dreamcast) ;;
    *) note_fail "es_visible_not_nes_dreamcast_only" ;;
  esac
fi

TH=$(vcgencmd get_throttled 2>/dev/null | tr -d '\r')
echo "THROTTLED=${TH:-}"
POWER=normal
WARNINGS=""
note_warn() { WARNINGS="${WARNINGS}|$1"; }
HEX=$(echo "$TH" | sed -n 's/.*throttled=0x\([0-9a-fA-F]*\).*/\1/p')
if [ -n "$HEX" ]; then
  VAL=$(printf '%d' "0x$HEX" 2>/dev/null || echo 0)
  # bit0 under-voltage now; bit2 throttled now; bit3 soft-temp now
  if [ $((VAL & 1)) -ne 0 ]; then
    POWER=low
    note_warn "power.low"
  elif [ $((VAL & 8)) -ne 0 ] || [ $((VAL & 4)) -ne 0 ]; then
    POWER=high
    note_warn "power.high"
  else
    POWER=normal
  fi
fi
echo "POWER=$POWER"

DMESG=$(dmesg 2>/dev/null | grep -Ei 'controller is down|I/O error' | tail -n 8 | tr '\n' ';' | tr -d '\r')
echo "DMESG_ALERTS=${DMESG:-}"
echo "$DMESG" | grep -qi 'controller is down' && note_fail "nvme_controller_down"
echo "$DMESG" | grep -qi 'I/O error' && note_fail "io_error"

CMDLINE=$(tr -d '\r' < /boot/cmdline.txt 2>/dev/null || true)
echo "CMDLINE=$CMDLINE"
echo "$CMDLINE" | grep -q 'nvme_core.default_ps_max_latency_us=0' || note_fail "cmdline_missing_nvme_ps"
echo "$CMDLINE" | grep -q 'pcie_aspm=off' || note_fail "cmdline_missing_aspm_off"

UP=$(uptime 2>/dev/null | tr -d '\r')
echo "UPTIME=$UP"
echo "WARNINGS=${WARNINGS#|}"
if [ -z "$FAILS" ]; then
  echo ARCADE_TEST_OK=1
else
  echo "ARCADE_TEST_OK=0"
  echo "ARCADE_TEST_FAILS=${FAILS#|}"
fi
echo ARCADE_TEST_END
'@
}

function Format-ArcadeTestStatus {
    param(
        [string[]]$Failures,
        [string[]]$Warnings,
        [string]$Power = 'normal',
        [bool]$SshOk = $true,
        [switch]$Ready
    )
    $warnList = New-Object System.Collections.Generic.List[string]
    foreach ($w in @($Warnings)) {
        if ($w) { $warnList.Add($w) | Out-Null }
    }

    if ($Ready) {
        $line = 'ssh=ok  video=ok  audio=ok  batocera=ok  scripts=ok'
        if ($warnList.Count -gt 0) {
            $line = $line + '  warning=' + ($warnList -join ',')
        }
        return $line
    }

    $set = @{}
    foreach ($item in @($Failures)) {
        if ($item) { $set[$item] = $true }
    }

    $ssh = 'ok'
    if (-not $SshOk -or $set.ContainsKey('ssh_unreachable')) {
        $ssh = 'down'
    }
    elseif ($set.ContainsKey('ping+22/SSH-stuck')) {
        $ssh = 'stuck'
    }
    elseif ($set.ContainsKey('ssh_fail')) {
        $ssh = 'err'
    }
    if ($ssh -ne 'ok') {
        return "ssh=$ssh  video=-  audio=-  batocera=-  scripts=-"
    }

    $video = if ($set.ContainsKey('hdmi_disconnected')) { 'down' } else { 'ok' }

    $audio = 'ok'
    if ($set.ContainsKey('audio_muted')) {
        $audio = 'mute'
    }
    elseif ($set.ContainsKey('pactl_default_null_or_empty') -or $set.ContainsKey('no_hdmi_sink_listed')) {
        $audio = 'dummy'
    }
    elseif (
        $set.ContainsKey('pactl_default_not_hdmi') -or
        $set.ContainsKey('audio_device_auto_or_empty') -or
        $set.ContainsKey('audio_device_not_hdmi') -or
        $set.ContainsKey('audio_profile_not_hdmi')
    ) {
        $audio = 'wait'
    }

    $batocera = 'ok'
    if ($set.ContainsKey('proc_emulationstation')) {
        $batocera = 'down'
    }
    elseif ($set.ContainsKey('es_systems_api_fail')) {
        $batocera = 'api'
    }
    elseif ($set.ContainsKey('es_visible_not_nes_dreamcast_only')) {
        $batocera = 'systems'
    }

    $scripts = 'ok'
    if (
        $set.ContainsKey('proc_main') -or
        $set.ContainsKey('service_main_off') -or
        $set.ContainsKey('runtime_main_tiny') -or
        $set.ContainsKey('arcade_main_tiny') -or
        $set.ContainsKey('venv_python_broken') -or
        $set.ContainsKey('proc_timer') -or
        $set.ContainsKey('service_timer_off') -or
        $set.ContainsKey('proc_server') -or
        $set.ContainsKey('service_server_off')
    ) {
        $scripts = 'down'
    }

    if ($set.ContainsKey('service_tvon_off')) { $warnList.Add('tvon.off') | Out-Null }
    if (
        $set.ContainsKey('runtime_tvon_tiny') -or
        $set.ContainsKey('arcade_tvon_tiny') -or
        $set.ContainsKey('tvon_cfg_missing') -or
        $set.ContainsKey('tvon_cfg_missing_soft_wait') -or
        $set.ContainsKey('tvon_cfg_missing_hotplug_suppress')
    ) {
        $warnList.Add('tvon.cfg') | Out-Null
    }
    if ($set.ContainsKey('nvme_controller_down') -or $set.ContainsKey('io_error')) {
        $warnList.Add('disk.io') | Out-Null
    }
    if ($set.ContainsKey('cmdline_missing_nvme_ps') -or $set.ContainsKey('cmdline_missing_aspm_off')) {
        $warnList.Add('cmdline') | Out-Null
    }

    $line = "ssh=$ssh  video=$video  audio=$audio  batocera=$batocera  scripts=$scripts"
    if ($warnList.Count -gt 0) {
        $line = $line + '  warning=' + ($warnList -join ',')
    }
    return $line
}

function Write-ArcadePowerEvent {
    param(
        [string]$Label,
        [string]$Phase,
        [string]$Stamp,
        [string]$Power,
        [ref]$LastPower
    )
    if ([string]::IsNullOrEmpty($Power)) { return }
    if ($LastPower.Value -eq $Power) { return }
    $prev = [string]$LastPower.Value
    $LastPower.Value = $Power
    if ($Power -eq 'low') {
        Write-ArcadeTestLog ("{0}  {1}  {2}  WARN  power=low" -f $Label, $Phase, $Stamp)
    }
    elseif ($Power -eq 'high') {
        Write-ArcadeTestLog ("{0}  {1}  {2}  WARN  power=high" -f $Label, $Phase, $Stamp)
    }
    elseif ($Power -eq 'normal' -and ($prev -eq 'low' -or $prev -eq 'high')) {
        Write-ArcadeTestLog ("{0}  {1}  {2}  power=normal" -f $Label, $Phase, $Stamp)
    }
}

function Invoke-ArcadeHealthProbe {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$ConnectTimeoutSec = 8
    )
    $ping = Test-Connection -ComputerName $HostName -Count 1 -Quiet -ErrorAction SilentlyContinue
    $tcp = Test-TcpPortOpen -HostName $HostName -Port 22 -TimeoutMs 800
    if (-not $ping -or -not $tcp) {
        return [pscustomobject]@{
            Ok            = $false
            SshOk         = $false
            Failures      = @('ssh_unreachable')
            Warnings      = @()
            Power         = ''
            TimerServerUp = $false
            Detail        = ''
            Text          = ''
        }
    }
    $script = Get-ArcadeTestProbeScript
    $script = ($script -replace "`r`n", "`n" -replace "`r", "`n")
    if (-not $script.EndsWith("`n")) { $script += "`n" }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
    $cmd = "echo $b64 | base64 -d | sh"
    $cap = Invoke-RemoteCapture -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -RemoteCommand $cmd -AskCmd $AskCmd -ConnectTimeoutSec $ConnectTimeoutSec
    if (-not $cap.Ok -or $cap.Text -notmatch 'ARCADE_TEST_END') {
        $pat = if ($ping -and $tcp) { 'ping+22/SSH-stuck' } else { 'ssh_fail' }
        return [pscustomobject]@{
            Ok            = $false
            SshOk         = $false
            Failures      = @($pat)
            Warnings      = @()
            Power         = ''
            TimerServerUp = $false
            Detail        = ''
            Text          = $cap.Text
        }
    }
    $fails = @()
    if ($cap.Text -match 'ARCADE_TEST_FAILS=(\S+)') {
        $fails = @($Matches[1].Split('|') | Where-Object { $_ })
    }
    $warns = @()
    if ($cap.Text -match '(?m)^WARNINGS=(\S*)') {
        $raw = $Matches[1].Trim()
        if ($raw) {
            $warns = @($raw.Split('|') | Where-Object { $_ })
        }
    }
    $power = 'normal'
    if ($cap.Text -match '(?m)^POWER=(\S+)') {
        $power = $Matches[1].Trim()
    }
    $timerServerUp = ($cap.Text -match '(?m)^TIMER_SERVER=up')
    $ok = ($cap.Text -match 'ARCADE_TEST_OK=1') -and ($fails.Count -eq 0)
    return [pscustomobject]@{
        Ok            = [bool]$ok
        SshOk         = $true
        Failures      = $fails
        Warnings      = $warns
        Power         = $power
        TimerServerUp = [bool]$timerServerUp
        Detail        = ''
        Text          = $cap.Text
    }
}

function Write-ArcadeTestLog {
    param([string]$Message)
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message)
}

function Format-ArcadeTestElapsed {
    param([datetime]$Start, [datetime]$Now = $(Get-Date), [int]$TotalSec = 0)
    $elapsed = [int][math]::Max(0, ($Now - $Start).TotalSeconds)
    $em = [int][math]::Floor($elapsed / 60)
    $es = $elapsed % 60
    if ($TotalSec -gt 0) {
        $tm = [int][math]::Floor($TotalSec / 60)
        $ts = $TotalSec % 60
        return ('{0}:{1:D2}/{2}:{3:D2}' -f $em, $es, $tm, $ts)
    }
    return ('{0}:{1:D2}' -f $em, $es)
}

function Wait-ArcadeBootWindow {
    param(
        [string]$Label,
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$Seconds = 300,
        [int]$IntervalSec = 10
    )
    Write-ArcadeTestLog ("{0}  boot  0:00/{1}:00  limit={2}s" -f $Label, ([int][math]::Floor($Seconds / 60)), $Seconds)
    $start = Get-Date
    $deadline = $start.AddSeconds($Seconds)
    $everOk = $false
    $readyAt = $null
    $lastStatus = ''
    $lastPower = ''
    $add5Done = $false
    while ((Get-Date) -lt $deadline) {
        $probe = Invoke-ArcadeHealthProbe -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName
        $stamp = Format-ArcadeTestElapsed -Start $start -TotalSec $Seconds
        Write-ArcadePowerEvent -Label $Label -Phase 'boot' -Stamp $stamp -Power $probe.Power -LastPower ([ref]$lastPower)

        # As soon as timer+server processes are up, start a session (do not wait for HDMI/audio).
        if (-not $add5Done -and $probe.TimerServerUp) {
            Write-ArcadeTestLog ("{0}  boot  {1}  timer=up  server=up  -> ADD_5" -f $Label, $stamp)
            Invoke-ArcadeTimerAdd5 -Label $Label -HostName $HostName
            $add5Done = $true
        }

        if ($probe.Ok) {
            if (-not $add5Done) {
                Write-ArcadeTestLog ("{0}  boot  {1}  timer=up  server=up  -> ADD_5" -f $Label, $stamp)
                Invoke-ArcadeTimerAdd5 -Label $Label -HostName $HostName
                $add5Done = $true
            }
            $everOk = $true
            $readyAt = Get-Date
            $status = Format-ArcadeTestStatus -Ready -Warnings $probe.Warnings -Power $probe.Power
            Write-ArcadeTestLog ("{0}  boot  {1}  {2}  READY" -f $Label, $stamp, $status)
            break
        }
        $status = Format-ArcadeTestStatus -Failures $probe.Failures -Warnings $probe.Warnings -Power $probe.Power -SshOk:$probe.SshOk
        if ($status -ne $lastStatus) {
            Write-ArcadeTestLog ("{0}  boot  {1}  {2}" -f $Label, $stamp, $status)
            $lastStatus = $status
        }
        $remain = [int][math]::Ceiling(($deadline - (Get-Date)).TotalSeconds)
        if ($remain -le 0) { break }
        Start-Sleep -Seconds ([math]::Min($IntervalSec, $remain))
    }
    if (-not $everOk) {
        $final = Invoke-ArcadeHealthProbe -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName
        $status = Format-ArcadeTestStatus -Failures $final.Failures -Warnings $final.Warnings -Power $final.Power -SshOk:$final.SshOk
        throw ("{0}  boot  FAIL  after {1}s  {2}" -f $Label, $Seconds, $status)
    }
    if (-not $add5Done) {
        throw ("{0}  boot  FAIL  timer/server never became ready for ADD_5" -f $Label)
    }
    $readySec = if ($readyAt) { [int]($readyAt - $start).TotalSeconds } else { $Seconds }
    Write-ArcadeTestLog ("{0}  boot  OK  ready_in={1}s  window={2}s" -f $Label, $readySec, $Seconds)
}

function Invoke-ArcadeTimerAdd5 {
    param(
        [string]$Label,
        [string]$HostName,
        [int]$Port = 5000,
        [int]$Tries = 6,
        [int]$TimeoutSec = 5
    )
    $url = ('http://{0}:{1}/test?action=ADD_5' -f $HostName, $Port)
    Write-ArcadeTestLog ("{0}  timer  ADD_5  -> {1}" -f $Label, $url)
    $lastErr = ''
    for ($i = 1; $i -le $Tries; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing -TimeoutSec $TimeoutSec
            $body = [string]$resp.Content
            if ($resp.StatusCode -eq 200 -and $body -match '"ok"\s*:\s*true') {
                Write-ArcadeTestLog ("{0}  timer  ADD_5  ok" -f $Label)
                return
            }
            $lastErr = ('http={0} body={1}' -f $resp.StatusCode, $body.Trim())
        }
        catch {
            $lastErr = $_.Exception.Message
        }
        Write-ArcadeTestLog ("{0}  timer  ADD_5  wait  try={1}/{2}  {3}" -f $Label, $i, $Tries, $lastErr)
        Start-Sleep -Seconds 2
    }
    throw ("{0}  timer  ADD_5  FAIL  {1}" -f $Label, $lastErr)
}

function Wait-ArcadeAliveWindow {
    param(
        [string]$Label,
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$Seconds = 300,
        [int]$IntervalSec = 15
    )
    Write-ArcadeTestLog ("{0}  alive  0:00/{1}:00  limit={2}s" -f $Label, ([int][math]::Floor($Seconds / 60)), $Seconds)
    $start = Get-Date
    $deadline = $start.AddSeconds($Seconds)
    $n = 0
    $lastProgressMin = -1
    $lastPower = ''
    $lastWarnKey = ''
    while ((Get-Date) -lt $deadline) {
        $n++
        $probe = Invoke-ArcadeHealthProbe -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName
        $stamp = Format-ArcadeTestElapsed -Start $start -TotalSec $Seconds
        Write-ArcadePowerEvent -Label $Label -Phase 'alive' -Stamp $stamp -Power $probe.Power -LastPower ([ref]$lastPower)
        if (-not $probe.Ok) {
            $status = Format-ArcadeTestStatus -Failures $probe.Failures -Warnings $probe.Warnings -Power $probe.Power -SshOk:$probe.SshOk
            Write-ArcadeTestLog ("{0}  alive  {1}  {2}  FAIL" -f $Label, $stamp, $status)
            throw ("{0}  alive  FAIL  probe#{1}  {2}" -f $Label, $n, $status)
        }
        $status = Format-ArcadeTestStatus -Ready -Warnings $probe.Warnings -Power $probe.Power
        $warnKey = (@($probe.Warnings) -join '|')
        $elapsedMin = [int][math]::Floor(((Get-Date) - $start).TotalSeconds / 60)
        if ($elapsedMin -ne $lastProgressMin -or $warnKey -ne $lastWarnKey) {
            Write-ArcadeTestLog ("{0}  alive  {1}  {2}" -f $Label, $stamp, $status)
            $lastProgressMin = $elapsedMin
            $lastWarnKey = $warnKey
        }
        $remain = [int][math]::Ceiling(($deadline - (Get-Date)).TotalSeconds)
        if ($remain -le 0) { break }
        Start-Sleep -Seconds ([math]::Min($IntervalSec, $remain))
    }
    Write-ArcadeTestLog ("{0}  alive  OK  {1}s" -f $Label, $Seconds)
}

function Test-ArcadeHostReachable {
    param([string]$HostName)
    $ping = Test-Connection -ComputerName $HostName -Count 1 -Quiet -ErrorAction SilentlyContinue
    $tcp = Test-TcpPortOpen -HostName $HostName -Port 22 -TimeoutMs 800
    return [bool]($ping -and $tcp)
}

function Wait-ArcadeSshReady {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$Seconds = 60,
        [int]$IntervalSec = 2,
        [string]$Phase = 'ssh'
    )
    Write-ArcadeTestLog ("{0}  0:00/{1}:00  limit={2}s" -f $Phase, ([int][math]::Floor($Seconds / 60)), $Seconds)
    $start = Get-Date
    $deadline = $start.AddSeconds($Seconds)
    $lastState = ''
    $lastPulseSec = -15
    while ((Get-Date) -lt $deadline) {
        $stamp = Format-ArcadeTestElapsed -Start $start -TotalSec $Seconds
        $ping = Test-Connection -ComputerName $HostName -Count 1 -Quiet -ErrorAction SilentlyContinue
        $tcp = Test-TcpPortOpen -HostName $HostName -Port 22 -TimeoutMs 800
        $state = 'ssh=down'
        if ($ping -and $tcp) {
            $hello = Invoke-RemoteCapture -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -RemoteCommand 'echo SSH_ALIVE' -AskCmd $AskCmd -ConnectTimeoutSec 5
            if ($hello.Ok -and $hello.Text -match 'SSH_ALIVE') {
                $sec = [int]((Get-Date) - $start).TotalSeconds
                Write-ArcadeTestLog ("{0}  {1}  ssh=ok  up_in={2}s" -f $Phase, $stamp, $sec)
                return
            }
            $state = 'ssh=wait'
        }
        elseif ($ping -or $tcp) {
            $state = 'net=partial'
        }
        $elapsed = [int]((Get-Date) - $start).TotalSeconds
        if ($state -ne $lastState -or ($elapsed - $lastPulseSec) -ge 15) {
            Write-ArcadeTestLog ("{0}  {1}  {2}" -f $Phase, $stamp, $state)
            $lastState = $state
            $lastPulseSec = $elapsed
        }
        $remain = [int][math]::Ceiling(($deadline - (Get-Date)).TotalSeconds)
        if ($remain -le 0) { break }
        Start-Sleep -Seconds ([math]::Min($IntervalSec, $remain))
    }
    throw ("{0}  FAIL  no SSH within {1}s" -f $Phase, $Seconds)
}

function Invoke-ArcadeRemoteReboot {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$OfflineWaitSec = 180,
        [int]$SshWaitSec = 60
    )
    # Batocera/Dropbear: фон + несколько fallback; повторяем, пока хост не уйдёт offline.
    $rebootSh = @'
set -e
echo REBOOT_SENT
sync || true
nohup sh -c "sleep 1; reboot -f 2>/dev/null || reboot 2>/dev/null || busybox reboot -f 2>/dev/null || shutdown -r now" >/dev/null 2>&1 &
exit 0
'@
    $rebootSh = ($rebootSh -replace "`r`n", "`n" -replace "`r", "`n")
    if (-not $rebootSh.EndsWith("`n")) { $rebootSh += "`n" }
    $rebootB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($rebootSh))
    $rebootCmd = "echo $rebootB64 | base64 -d | sh"

    $offDeadline = (Get-Date).AddSeconds($OfflineWaitSec)
    $offStart = Get-Date
    $lastOffLog = -10
    $lastKick = [datetime]::MinValue
    $kick = 0
    $sawOffline = $false

    while ((Get-Date) -lt $offDeadline) {
        $ping = Test-Connection -ComputerName $HostName -Count 1 -Quiet -ErrorAction SilentlyContinue
        $tcp = Test-TcpPortOpen -HostName $HostName -Port 22 -TimeoutMs 700
        # Offline = SSH port gone (ping alone can linger on some networks).
        if (-not $tcp) {
            $sawOffline = $true
            Write-ArcadeTestLog ('reboot  offline  tcp22=down  ping={0}' -f ($(if ($ping) { 'up' } else { 'down' })))
            break
        }

        $elapsed = [int]((Get-Date) - $offStart).TotalSeconds
        $needKick = ($kick -eq 0) -or (((Get-Date) - $lastKick).TotalSeconds -ge 20)
        if ($needKick -and $kick -lt 6) {
            $kick++
            $lastKick = Get-Date
            Write-ArcadeTestLog ("reboot  restarting…  try={0}/6" -f $kick)
            try {
                $cap = Invoke-RemoteCapture -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -RemoteCommand $rebootCmd -AskCmd $AskCmd -ConnectTimeoutSec 12
                if ($cap.Text -match 'REBOOT_SENT') {
                    Write-ArcadeTestLog 'reboot  command accepted'
                }
                elseif (-not $cap.Ok) {
                    # Drop mid-command often means reboot already started.
                    Write-ArcadeTestLog ('reboot  ssh dropped (exit={0}) — waiting offline' -f $cap.ExitCode)
                }
                else {
                    Write-ArcadeTestLog ('reboot  no REBOOT_SENT  out={0}' -f $cap.Text.Trim())
                }
            }
            catch {
                Write-ArcadeTestLog ('reboot  ssh error — waiting offline  {0}' -f $_.Exception.Message)
            }
        }

        if (($elapsed - $lastOffLog) -ge 10) {
            Write-ArcadeTestLog ("reboot  waiting offline  {0}s  tcp22=up  ping={1}" -f $elapsed, ($(if ($ping) { 'up' } else { 'down' })))
            $lastOffLog = $elapsed
        }
        Start-Sleep -Seconds 2
    }
    if (-not $sawOffline) {
        throw 'reboot  FAIL  still online'
    }

    Start-Sleep -Seconds 3
    Wait-ArcadeSshReady -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName -Seconds $SshWaitSec -Phase 'ssh'
}

function Start-ArcadeTestCycle {
    param(
        [string]$Label,
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName,
        [int]$SshWaitSec = 60,
        [int]$BootSec = 300,
        [int]$AliveSec = 300
    )
    # Reachability only (ping+tcp). Do not SSH here — Dropbear can hang after long cycles.
    Write-ArcadeTestLog ("{0}  probe…" -f $Label)
    $online = Test-ArcadeHostReachable -HostName $HostName
    if ($online) {
        Write-ArcadeTestLog ("{0}  cabinet=online  -> reboot" -f $Label)
        Invoke-ArcadeRemoteReboot -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName -SshWaitSec $SshWaitSec
    }
    else {
        Write-ArcadeTestLog ("{0}  cabinet=offline  -> wait SSH" -f $Label)
        Wait-ArcadeSshReady -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName -Seconds $SshWaitSec -Phase 'ssh'
    }

    Wait-ArcadeBootWindow -Label $Label -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName -Seconds $BootSec -IntervalSec 10
    Write-ArcadeTestLog ("{0}  -> alive" -f $Label)
    Wait-ArcadeAliveWindow -Label $Label -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName -Seconds $AliveSec -IntervalSec 15
    Write-ArcadeTestLog ("{0}  PASS" -f $Label)
}

function Invoke-ArcadeCabinetTest {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$AskCmd,
        [string]$HostName
    )
    $cycles = @('cycle1', 'cycle2', 'final')
    Write-Host ''
    Write-Host '========== ARCADE TEST =========='
    Write-Host 'if online -> reboot; SSH <=1m; boot <=5m (ADD_5 when timer+server up); alive 5m'
    Write-Host 'cycles: x3 (reboot between when online)'
    Write-Host '================================='
    $i = 0
    foreach ($label in $cycles) {
        $i++
        Write-Host ''
        Write-ArcadeTestLog ("---- {0} ({1}/{2}) ----" -f $label, $i, $cycles.Count)
        Start-ArcadeTestCycle -Label $label -SshTarget $SshTarget -SshBaseArgs $SshBaseArgs -AskCmd $AskCmd -HostName $HostName
    }
    Write-Host ''
    Write-ArcadeTestLog 'RESULT  all cycles PASS'
}

$config = Get-ArcadeMachinesConfig
$cabinetHost = Resolve-ArcadeHost -Name $Target -Config $config

if (-not $SshUser) {
    if ($config -and $config.user) {
        $SshUser = [string]$config.user
    }
    else {
        $SshUser = 'root'
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
Write-Host '-> Test: reboot-if-online, SSH<=1m, boot 5m + alive 5m x3'

$askCmd = New-ArcadeAskPass -Text $Auth
try {
    Invoke-ArcadeCabinetTest -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -AskCmd $askCmd -HostName $cabinetHost
}
finally {
    Remove-Item -LiteralPath $askCmd -Force -ErrorAction SilentlyContinue
    $askPs1Path = [System.IO.Path]::ChangeExtension($askCmd, '.ps1')
    if ($askPs1Path) {
        Remove-Item -LiteralPath $askPs1Path -Force -ErrorAction SilentlyContinue
    }
}
