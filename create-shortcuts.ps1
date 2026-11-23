param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$icon = Join-Path $root "static\icon.ico"

# Prefer the windowless interpreter: no console window when the shortcut is used.
$pythonw = Join-Path $root "python\pythonw.exe"
$target = $null
$argumentList = ""

if (Test-Path $pythonw) {
    $target = $pythonw
    $argumentList = "launcher.py"
} else {
    foreach ($name in @("2-启动搜索.cmd", "start-search.cmd")) {
        $candidate = Join-Path $root $name
        if (Test-Path $candidate) { $target = $candidate; break }
    }
}

if ($null -eq $target) {
    if (-not $Quiet) { Write-Host "找不到启动脚本，无法创建快捷方式。" -ForegroundColor Red }
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param([string]$Path)
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $target
    if ($argumentList) { $shortcut.Arguments = $argumentList }
    $shortcut.WorkingDirectory = $root
    if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
    $shortcut.Description = "极目 · Telegram 群组检索"
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

$created = @()

$desktop = [Environment]::GetFolderPath("Desktop")
if ($desktop -and (Test-Path $desktop)) {
    $path = Join-Path $desktop "极目.lnk"
    New-Shortcut -Path $path
    $created += $path
}

$programs = [Environment]::GetFolderPath("Programs")
if ($programs -and (Test-Path $programs)) {
    $path = Join-Path $programs "极目.lnk"
    New-Shortcut -Path $path
    $created += $path
}

if (-not $Quiet) {
    if ($created.Count -eq 0) {
        Write-Host "没有找到桌面或开始菜单目录，未创建快捷方式。" -ForegroundColor Yellow
    } else {
        Write-Host "已创建快捷方式：" -ForegroundColor Green
        foreach ($item in $created) { Write-Host "  $item" }
        Write-Host ""
        Write-Host "以后从桌面或开始菜单点「极目」就能打开。移动文件夹后重新运行本脚本即可。"
    }
}
