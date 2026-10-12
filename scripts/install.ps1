<#
.SYNOPSIS
    Installiert JARVIS unter Windows 11 - komplett kostenlos.

.DESCRIPTION
    - sucht Python 3.11+ (bevorzugt 3.12) ueber den Python-Launcher "py"
    - prueft Git (fuer die Selbstverbesserung) und bietet die Installation per winget an
    - legt die virtuelle Umgebung .venv an und installiert alle Pakete
    - bietet Claude Code (nutzt dein Claude-Pro-Abo) und Ollama (lokale KI) per winget an
    - legt eine Verknuepfung "JARVIS" auf dem Desktop und im Startmenue an
    - startet die gefuehrte Einrichtung (Name, Wohnort, Gemini-Key, Google, Autostart)

    Das Skript kann jederzeit erneut ausgefuehrt werden (z. B. nach einem Update).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -Dev          # zusaetzlich pytest, ruff, mypy
    .\scripts\install.ps1 -NoSetup      # ohne gefuehrte Einrichtung
    .\scripts\install.ps1 -Yes          # alle Fragen mit "Ja" beantworten
#>
param(
    [switch]$Dev,
    [switch]$NoSetup,
    [switch]$Yes,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    # Native Programme schreiben Hinweise nach stderr; das darf nicht abbrechen.
    $ErrorActionPreference = "Continue"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Befehl fehlgeschlagen ($LASTEXITCODE): $Exe $($Arguments -join ' ')"
    }
}

function Confirm-Step {
    param([string]$Question, [bool]$Default = $true)
    if ($Yes) { return $true }
    $hint = if ($Default) { "J/n" } else { "j/N" }
    $answer = Read-Host "$Question ($hint)"
    if (-not $answer) { return $Default }
    return @("j", "ja", "y", "yes") -contains $answer.Trim().ToLower()
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WithWinget {
    param([string]$Id, [string]$Name)
    if (-not (Test-Command "winget")) {
        Write-Host "winget fehlt - bitte $Name manuell installieren." -ForegroundColor Yellow
        return $false
    }
    Write-Host "Installiere $Name ..."
    $ErrorActionPreference = "Continue"
    & winget install -e --id $Id --accept-package-agreements --accept-source-agreements
    $ok = $LASTEXITCODE -eq 0
    # PATH der laufenden Sitzung auffrischen, damit neue Programme sofort gefunden werden
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
        [Environment]::GetEnvironmentVariable("Path", "User")
    return $ok
}

function Find-Python {
    # Reihenfolge: 3.12 (beste Paketunterstuetzung), 3.11, 3.13, dann "python"
    $ErrorActionPreference = "Continue"
    $candidates = @("py -3.12", "py -3.11", "py -3.13", "python")
    foreach ($candidate in $candidates) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        $pyArgs = @()
        if ($parts.Length -gt 1) { $pyArgs = @($parts[1..($parts.Length - 1)]) }
        if (-not (Test-Command $exe)) { continue }
        try {
            $version = & $exe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
        $major, $minor = $version.Trim().Split(".")
        if ([int]$major -eq 3 -and [int]$minor -ge 11) {
            return @{ Exe = $exe; Args = $pyArgs; Version = $version.Trim() }
        }
    }
    return $null
}

function New-Shortcut {
    param([string]$Path, [string]$Target, [string]$Arguments, [string]$Icon)
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Target
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $Root
    $link.Description = "JARVIS - persoenlicher KI-Assistent"
    if ($Icon -and (Test-Path $Icon)) { $link.IconLocation = $Icon }
    $link.Save()
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  J.A.R.V.I.S. - Installation (kostenlos)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# --- 1. Python -------------------------------------------------------------
if ($Python) {
    $py = @{ Exe = $Python; Args = @(); Version = "?" }
} else {
    $py = Find-Python
}
if (-not $py) {
    Write-Host "Kein Python 3.11+ gefunden." -ForegroundColor Yellow
    if (Confirm-Step "Python 3.12 jetzt mit winget installieren?") {
        Install-WithWinget "Python.Python.3.12" "Python 3.12" | Out-Null
        $py = Find-Python
    }
}
if (-not $py) {
    Write-Host "Bitte Python 3.12 installieren (winget install Python.Python.3.12) und ein NEUES Fenster oeffnen." -ForegroundColor Red
    exit 1
}
Write-Host "[1/6] Python $($py.Version): $($py.Exe) $($py.Args -join ' ')"

# --- 2. Git -----------------------------------------------------------------
if (Test-Command "git") {
    Write-Host "[2/6] Git ist installiert."
} else {
    Write-Host "[2/6] Git fehlt - wird fuer die Selbstverbesserung gebraucht."
    if (Confirm-Step "Git jetzt mit winget installieren?") {
        Install-WithWinget "Git.Git" "Git" | Out-Null
    }
}

# --- 3. Pakete --------------------------------------------------------------
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[3/6] Erstelle virtuelle Umgebung .venv ..."
    Invoke-Checked $py.Exe (@($py.Args) + @("-m", "venv", ".venv"))
} else {
    Write-Host "[3/6] Virtuelle Umgebung vorhanden."
}
Write-Host "      Installiere Pakete (beim ersten Mal einige Minuten) ..."
Invoke-Checked $venvPython @("-m", "pip", "install", "--upgrade", "pip", "--quiet")
$requirements = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
Invoke-Checked $venvPython @("-m", "pip", "install", "-r", $requirements)
Invoke-Checked $venvPython @("-m", "pip", "install", "--no-deps", "-e", ".", "--quiet")

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
}

# --- 4. Claude Code (Pro-Abo) und Ollama -----------------------------------
$claudeLocal = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
if ((Test-Command "claude") -or (Test-Path $claudeLocal)) {
    Write-Host "[4/6] Claude Code ist installiert."
} else {
    Write-Host "[4/6] Claude Code fehlt. Damit nutzt JARVIS dein Claude-Pro-Abo - ohne API-Kosten,"
    Write-Host "      auch fuer die Selbstverbesserung."
    if (Confirm-Step "Claude Code jetzt mit winget installieren?") {
        Install-WithWinget "Anthropic.ClaudeCode" "Claude Code" | Out-Null
    }
}
if (Test-Command "ollama") {
    Write-Host "      Ollama ist installiert."
} else {
    Write-Host "      Ollama (lokale KI, offline und privat) ist optional, braucht aber 2-20 GB Platz."
    if (Confirm-Step "Ollama jetzt mit winget installieren?" $false) {
        Install-WithWinget "Ollama.Ollama" "Ollama" | Out-Null
    }
}

# --- 5. Verknuepfungen --------------------------------------------------------
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$icon = Join-Path $Root "jarvis\ui\web\static\icon.ico"
try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "JARVIS.lnk"
    New-Shortcut (Join-Path $desktop "JARVIS.lnk") $pythonw "-m jarvis start" $icon
    New-Shortcut $startMenu $pythonw "-m jarvis start" $icon
    Write-Host "[5/6] Verknuepfung 'JARVIS' auf dem Desktop und im Startmenue angelegt."
} catch {
    Write-Host "[5/6] Verknuepfung konnte nicht angelegt werden: $_" -ForegroundColor Yellow
}

# --- 6. Einrichtung -------------------------------------------------------------
Write-Host ""
if ($NoSetup) {
    Write-Host "[6/6] Einrichtung uebersprungen (spaeter: .\scripts\jarvis.cmd setup)"
} else {
    Write-Host "[6/6] Gefuehrte Einrichtung ..."
    $ErrorActionPreference = "Continue"
    & $venvPython -m jarvis setup
}

Write-Host ""
Write-Host "Fertig!" -ForegroundColor Green
Write-Host "  JARVIS starten:   Doppelklick auf 'JARVIS' (Desktop) oder  .\scripts\jarvis.cmd"
Write-Host "  Dashboard:        http://127.0.0.1:8765  (oeffnet sich automatisch)"
Write-Host "  Not-Aus:          Strg+Alt+Umschalt+X"
Write-Host ""
if (Confirm-Step "JARVIS jetzt starten?") {
    & $venvPython -m jarvis start
}
