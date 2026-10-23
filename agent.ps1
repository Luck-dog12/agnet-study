$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    Write-Error "Startup failed: project virtual environment not found."
    exit 2
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -m app.main @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
