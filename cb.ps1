#!/usr/bin/env pwsh
# cb — Claude Browser CLI wrapper.
# Forwards all args to cb.py with the local Python interpreter.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& python (Join-Path $scriptDir "cb.py") @args
exit $LASTEXITCODE
