param(
    [switch]$Clean,
    [switch]$Sign,
    [string]$CertificatePath,
    [string]$CertificatePassword,
    [string]$CertificateThumbprint,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$SignToolPath
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$appName = "TempHumidityLogger"
$distAppDir = Join-Path $projectRoot "dist\$appName"
$runtimeRoot = $env:LOCALAPPDATA
if (-not $runtimeRoot) {
    $runtimeRoot = $projectRoot
}
$runtimeDataDir = Join-Path $runtimeRoot $appName

function Backup-LegacyRuntimeData {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$DestinationDir
    )

    if (-not (Test-Path $SourceDir)) {
        return
    }

    New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
    $patterns = @("config.ini", "logger.db*", "*.csv")
    foreach ($pattern in $patterns) {
        $items = Get-ChildItem -Path $SourceDir -Filter $pattern -File -ErrorAction SilentlyContinue
        foreach ($item in $items) {
            Copy-Item -Path $item.FullName -Destination (Join-Path $DestinationDir $item.Name) -Force
        }
    }
}

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

Backup-LegacyRuntimeData -SourceDir $distAppDir -DestinationDir $runtimeDataDir

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

function Resolve-SignTool {
    if ($SignToolPath) {
        if (Test-Path $SignToolPath) {
            return (Resolve-Path $SignToolPath).Path
        }
        throw "SignTool not found at: $SignToolPath"
    }

    $pathSignTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($pathSignTool) {
        return $pathSignTool.Source
    }

    $kitRoots = @(
        (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"),
        (Join-Path $env:ProgramFiles "Windows Kits\10\bin")
    ) | Where-Object { $_ -and (Test-Path $_) }

    $candidates = @()
    foreach ($root in $kitRoots) {
        $candidates += Get-ChildItem -Path $root -Recurse -Filter signtool.exe -File -ErrorAction SilentlyContinue
    }

    $preferred = $candidates |
        Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
        Sort-Object FullName -Descending |
        Select-Object -First 1

    if (-not $preferred) {
        $preferred = $candidates |
            Sort-Object FullName -Descending |
            Select-Object -First 1
    }

    if ($preferred) {
        return $preferred.FullName
    }

    throw "signtool.exe was not found. Install the Windows SDK or pass -SignToolPath."
}

function Invoke-CodeSign {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath
    )

    if (-not (Test-Path $FilePath)) {
        throw "Cannot sign missing file: $FilePath"
    }

    $signtool = Resolve-SignTool
    $signArgs = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")

    if ($CertificatePath) {
        if (-not (Test-Path $CertificatePath)) {
            throw "Certificate file not found: $CertificatePath"
        }
        $signArgs += @("/f", $CertificatePath)
        if ($CertificatePassword) {
            $signArgs += @("/p", $CertificatePassword)
        }
    } elseif ($CertificateThumbprint) {
        $signArgs += @("/sha1", $CertificateThumbprint)
    } else {
        $signArgs += "/a"
    }

    $signArgs += $FilePath

    Write-Host ""
    Write-Host "Signing executable:"
    Write-Host "  $FilePath"
    & $signtool @signArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Code signing failed."
    }

    & $signtool verify /pa /v $FilePath
    if ($LASTEXITCODE -ne 0) {
        throw "Code signing verification failed."
    }
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
        & $pythonExe -m pip install -r requirements.txt
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
    --onedir `
    --contents-directory "libraries" `
    --windowed `
    --noupx `
    --name "TempHumidityLogger" `
    --icon "icons\logo.ico" `
    --add-data "icons\logo.ico;icons" `
    --add-data "icons\logo.png;icons" `
    --add-data "icons\logo1.png;icons" `
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
    temp_humidity_logger\main.py

$exePath = Join-Path $distAppDir "$appName.exe"
if ($Sign) {
    Invoke-CodeSign -FilePath $exePath
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $exePath"
if ($Sign) {
    Write-Host "Signed:"
    Write-Host "  $exePath"
}
Write-Host "Libraries:"
Write-Host "  $projectRoot\dist\TempHumidityLogger\libraries"
Write-Host "Runtime data:"
Write-Host "  $runtimeDataDir"
