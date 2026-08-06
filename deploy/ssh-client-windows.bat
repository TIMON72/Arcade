@echo off
setlocal
REM One-time: SSH client for Arcade/Batocera fleet (LAN).
REM Edit SUBNET if needed, then run (double-click or cmd).

set "SUBNET=192.168.1.*"
set "CFG=%USERPROFILE%\.ssh\config"

mkdir "%USERPROFILE%\.ssh" 2>nul

(
echo Host batocera BATOCERA
echo   HostName batocera
echo   User root
echo   AddressFamily inet
echo   Ciphers aes128-ctr,aes256-ctr,chacha20-poly1305@openssh.com
echo   MACs hmac-sha2-256,hmac-sha1
echo   StrictHostKeyChecking no
echo   UserKnownHostsFile NUL
echo   LogLevel ERROR
echo.
echo Host %SUBNET%
echo   User root
echo   StrictHostKeyChecking no
echo   UserKnownHostsFile NUL
echo   LogLevel ERROR
) > "%CFG%"

echo Wrote %CFG%
type "%CFG%"
echo.
pause
