#!/usr/bin/env pwsh
# cb - Claude Browser CLI wrapper (PowerShell). Prefers the harness's own .venv.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:CB_HOME = $scriptDir
& $py -c "import os, sys; sys.path.insert(0, os.environ['CB_HOME']); import cb; sys.exit(cb.main())" @args
exit $LASTEXITCODE
