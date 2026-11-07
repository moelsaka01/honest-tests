# Record the real demo in its own terminal window with ffmpeg (Windows), then build assets/demo.gif.
#   .\tools\record_demo.ps1                 # Python demo (about 45 seconds)
#   .\tools\record_demo.ps1 -Demo js
# Only that window's visible rectangle is captured (never the whole screen). Do not touch the mouse or
# keyboard while it runs: a terminal window appears on screen and is closed at the end.
# Needs: ffmpeg on PATH, python on PATH, git. Output: assets/demo.gif.
param(
    [ValidateSet('python', 'js')][string]$Demo = 'python',
    [int]$Show = 6,
    [int]$TimeoutSec = 240
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $env:TEMP "honest-tests-rec"
New-Item -ItemType Directory -Force $work, (Join-Path $root 'assets') | Out-Null
$title = "honest-tests-demo"
$go = Join-Path $work "go.flag"; $done = Join-Path $work "done.flag"
$mkv = Join-Path $work "demo.mkv"
$gif = Join-Path $root "assets\demo.gif"
Remove-Item $go, $done, $mkv -ErrorAction SilentlyContinue
Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue   # let the demo show its colours

Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public struct RECT { public int L, T, R, B; }
public class Win {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool r);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
}
'@
[void][Win]::SetProcessDPIAware()

$launcher = Join-Path $work "launch.cmd"
@"
@echo off
title $title
mode con: cols=104 lines=40
cd /d "$root"
:wait
if not exist "$go" (ping -n 2 127.0.0.1 >nul & goto wait)
cls
python examples\run_demo.py $Demo --pace --show $Show
echo done> "$done"
ping -n 4 127.0.0.1 >nul
"@ | Set-Content -Encoding ASCII $launcher

$term = Start-Process cmd -ArgumentList '/c', "`"$launcher`"" -PassThru
$win = $null
for ($i = 0; $i -lt 60 -and -not $win; $i++) {
    Start-Sleep -Milliseconds 300
    $win = Get-Process | Where-Object { $_.MainWindowTitle -like "*$title*" -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
}
if (-not $win) { throw "terminal window '$title' did not appear" }
$h = $win.MainWindowHandle
$sw = [Win]::GetSystemMetrics(0); $sh = [Win]::GetSystemMetrics(1)
$w = [Math]::Min(1500, $sw - 80); $hh = [Math]::Min(1000, $sh - 80)
[void][Win]::MoveWindow($h, 40, 20, $w, $hh, $true); [void][Win]::SetForegroundWindow($h)
Start-Sleep -Seconds 2
$r = New-Object RECT
[void][Win]::DwmGetWindowAttribute($h, 9, [ref]$r, 16)   # exact visible bounds
$cw = $r.R - $r.L; $ch = $r.B - $r.T
if ($cw % 2) { $cw-- }; if ($ch % 2) { $ch-- }
"window: $($r.L),$($r.T) ${cw}x${ch}  screen: ${sw}x${sh}"

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = 'ffmpeg'
$psi.Arguments = "-hide_banner -loglevel error -y -f gdigrab -framerate 10 -offset_x $($r.L) -offset_y $($r.T) -video_size ${cw}x${ch} -i desktop -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -g 10 `"$mkv`""
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.CreateNoWindow = $true
$ff = [System.Diagnostics.Process]::Start($psi)
Start-Sleep -Seconds 1
Set-Content $go "go"
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while (-not (Test-Path $done) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
Start-Sleep -Seconds 2
$ff.StandardInput.WriteLine('q')                          # graceful stop: ffmpeg finalises the file
if (-not $ff.WaitForExit(20000)) { $ff.Kill() }
Stop-Process -Id $win.Id -Force -ErrorAction SilentlyContinue
if (-not (Test-Path $done)) { throw "demo did not finish within $TimeoutSec s" }

# mkv -> palette-optimised GIF
ffmpeg -hide_banner -loglevel error -y -i $mkv -vf "fps=8,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5" -loop 0 $gif
"{0}  {1:N1} MB" -f $gif, ((Get-Item $gif).Length / 1MB)
