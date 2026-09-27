param(
    [Parameter(Mandatory = $true)]
    [string]$Repo
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $Repo).Path
$startBat = Join-Path $repo "start.bat"
if (-not (Test-Path -LiteralPath $startBat)) {
    throw "start.bat was not found in $repo"
}

$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop) {
    $desktop = Join-Path $env:USERPROFILE "Desktop"
}

$launcher = Join-Path $desktop "SISU.bat"
$startLine = "@echo off`r`ncd /d `"$repo`"`r`ncall `"$startBat`"`r`n"
[System.IO.File]::WriteAllText($launcher, $startLine, [System.Text.Encoding]::ASCII)

$icon = Join-Path $env:SystemRoot "System32\imageres.dll,14"
foreach ($name in @("pythonw.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $icon = "$($cmd.Source),0"
        break
    }
}

$shell = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path $desktop "SISU.lnk"
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $startBat
$shortcut.WorkingDirectory = $repo
$shortcut.WindowStyle = 1
$shortcut.Description = "SISU Book Catalog Filler"
$shortcut.IconLocation = $icon
$shortcut.Save()

Write-Host "Created $launcher"
Write-Host "Created $shortcutPath"
