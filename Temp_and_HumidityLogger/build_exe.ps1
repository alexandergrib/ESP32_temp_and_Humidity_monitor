param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$pythonExe = Join-Path $projectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $answer = (Read-Host "Python venv not found. Create it now? (y/n)").Trim().ToLowerInvariant()
    if ($answer -ne "y" -and $answer -ne "yes") {
        throw "Python venv not found at: $pythonExe"
    }

    $bootstrapPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $bootstrapPython) {
        throw "System Python not found in PATH. Install Python 3 and rerun."
    }

    & $bootstrapPython.Source -m venv "$projectRoot\venv"
    if (-not (Test-Path $pythonExe)) {
        throw "Failed to create venv at: $projectRoot\venv"
    }
}

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$projectRoot\build"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$projectRoot\dist"
    Remove-Item -Force -ErrorAction SilentlyContinue "$projectRoot\TempHumidityLogger.spec"
}

function Test-PythonModule {
    param(
        [Parameter(Mandatory = $true)][string]$ModuleName
    )
    & $pythonExe -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)"
    return ($LASTEXITCODE -eq 0)
}

$requiredModules = @("serial", "matplotlib", "PyInstaller")
$missingModules = @()
foreach ($m in $requiredModules) {
    if (-not (Test-PythonModule -ModuleName $m)) {
        $missingModules += $m
    }
}

if ($missingModules.Count -gt 0) {
    Write-Host "Missing dependencies: $($missingModules -join ', ')"
    $installAnswer = (Read-Host "Install missing dependencies automatically now? (y/n)").Trim().ToLowerInvariant()
    if ($installAnswer -eq "y" -or $installAnswer -eq "yes") {
        & $pythonExe -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upgrade pip."
        }
        & $pythonExe -m pip install -r requirements.txt pyinstaller
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install required dependencies."
        }
    } else {
        throw "Missing dependencies were not installed. Aborting build."
    }
}

# Validate Tk support and help PyInstaller find Tcl/Tk runtime.
& $pythonExe -c "import tkinter, _tkinter; print('tkinter ok')"
if ($LASTEXITCODE -ne 0) {
    throw "tkinter is not available in this Python environment. Install Python with Tcl/Tk support and rebuild."
}

$basePrefix = (& $pythonExe -c "import sys; print(sys.base_prefix)").Trim()
$tclDir = Join-Path $basePrefix "tcl\\tcl8.6"
$tkDir = Join-Path $basePrefix "tcl\\tk8.6"
if (Test-Path $tclDir) { $env:TCL_LIBRARY = $tclDir }
if (Test-Path $tkDir)  { $env:TK_LIBRARY  = $tkDir }

& $pythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --noupx `
    --name "TempHumidityLogger" `
    --add-data "icons\logo.png;icons" `
    --collect-all matplotlib `
    --collect-all numpy `
    --collect-all PIL `
    --collect-all serial `
    --collect-all dateutil `
    --collect-all packaging `
    --collect-all kiwisolver `
    --collect-all cycler `
    --collect-all pyparsing `
    --hidden-import tkinter `
    --hidden-import _tkinter `
    --hidden-import serial.tools.list_ports `
    --hidden-import serial.tools.list_ports_common `
    --hidden-import serial.tools.list_ports_windows `
    arduino_logger_v72.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $projectRoot\dist\TempHumidityLogger.exe"
