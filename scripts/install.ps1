<#
.SYNOPSIS
    Installiert JARVIS (Phase 1) unter Windows 11.

.DESCRIPTION
    - sucht Python 3.11+ (bevorzugt 3.12) ueber den Python-Launcher "py"
    - legt die virtuelle Umgebung .venv an
    - installiert alle Abhaengigkeiten und JARVIS selbst (Befehl "jarvis")
    - kopiert .env.example nach .env (falls noch nicht vorhanden)
    - zeigt die erkannte Hardware

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -Dev        # zusaetzlich pytest, ruff, mypy
#>
param(
    [switch]$Dev,
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

function Find-Python {
    # Reihenfolge: 3.12 (beste Paketunterstuetzung), 3.11, 3.13, dann "python"
    $ErrorActionPreference = "Continue"
    $candidates = @("py -3.12", "py -3.11", "py -3.13", "python")
    foreach ($candidate in $candidates) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        $pyArgs = @()
        if ($parts.Length -gt 1) { $pyArgs = @($parts[1..($parts.Length - 1)]) }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
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

Write-Host "== JARVIS Installation ==" -ForegroundColor Cyan

if ($Python) {
    $py = @{ Exe = $Python; Args = @(); Version = "?" }
} else {
    $py = Find-Python
}
if (-not $py) {
    Write-Host "Kein Python 3.11+ gefunden." -ForegroundColor Red
    Write-Host "Installiere Python 3.12, z. B. mit:  winget install Python.Python.3.12"
    exit 1
}
Write-Host "Python $($py.Version): $($py.Exe) $($py.Args -join ' ')"

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Erstelle virtuelle Umgebung .venv ..."
    Invoke-Checked $py.Exe (@($py.Args) + @("-m", "venv", ".venv"))
}

Write-Host "Installiere Abhaengigkeiten ..."
Invoke-Checked $venvPython @("-m", "pip", "install", "--upgrade", "pip")
if ($Dev) {
    Invoke-Checked $venvPython @("-m", "pip", "install", "-r", "requirements-dev.txt")
} else {
    Invoke-Checked $venvPython @("-m", "pip", "install", "-r", "requirements.txt")
}
Invoke-Checked $venvPython @("-m", "pip", "install", "--no-deps", "-e", ".")

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
    Write-Host "Datei .env angelegt (leer) - Keys besser per 'jarvis secrets set' speichern."
}

Write-Host ""
Invoke-Checked $venvPython @("-m", "jarvis", "hw")

Write-Host ""
Write-Host "Fertig." -ForegroundColor Green
Write-Host "1. API-Key speichern:   .\scripts\jarvis.cmd secrets set ANTHROPIC_API_KEY"
Write-Host "2. Pruefen:             .\scripts\jarvis.cmd check --live"
Write-Host "3. Chat starten:        .\scripts\jarvis.cmd"
