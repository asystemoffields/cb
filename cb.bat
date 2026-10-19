@echo off
rem cb - Claude Browser CLI wrapper (Windows cmd). Prefers the harness's own .venv.
setlocal
set "CB_HOME=%~dp0."
set "CB_PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "CB_PY=%~dp0.venv\Scripts\python.exe"
"%CB_PY%" -c "import os, sys; sys.path.insert(0, os.environ['CB_HOME']); import cb; sys.exit(cb.main())" %*
exit /b %ERRORLEVEL%
