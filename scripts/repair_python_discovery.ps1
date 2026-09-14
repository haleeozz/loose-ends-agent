$ErrorActionPreference = "Stop"

$pythonBin = Join-Path $env:LOCALAPPDATA "Python\bin"
$pythonExe = Join-Path $pythonBin "python.exe"
$userBin = Join-Path $env:USERPROFILE "bin"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Working user Python was not found at $pythonExe"
}

New-Item -ItemType Directory -Path $userBin -Force | Out-Null

$shim = @'
@echo off
"%LOCALAPPDATA%\Python\bin\python.exe" %*
'@

Set-Content -LiteralPath (Join-Path $userBin "python.cmd") -Value $shim -Encoding Ascii
Set-Content -LiteralPath (Join-Path $userBin "py.cmd") -Value $shim -Encoding Ascii

$preferred = @("%LOCALAPPDATA%\Python\bin", "%USERPROFILE%\bin")
$current = [Environment]::GetEnvironmentVariable("Path", "User")
$remaining = @($current -split ";" | Where-Object {
    $entry = $_.Trim()
    if (-not $entry) { return $false }
    $expanded = [Environment]::ExpandEnvironmentVariables($entry).TrimEnd("\")
    return $expanded -notin @($pythonBin.TrimEnd("\"), $userBin.TrimEnd("\"))
})
$newPath = (@($preferred) + $remaining) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")

Write-Output "Updated user PATH: $newPath"
Write-Output "python shim: $(Join-Path $userBin 'python.cmd')"
Write-Output "py compatibility shim: $(Join-Path $userBin 'py.cmd')"
& $pythonExe --version
