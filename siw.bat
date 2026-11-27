@echo off
setlocal EnableExtensions
rem ========================================================================
rem  siw.bat - the SIW front door
rem
rem  Starts the console server (kernel API + web console, one port, 7662 by
rem  default). The console opens in your browser unless the config sets
rem  open_browser to false. The server runs in THIS window until you press Ctrl-C.
rem
rem    siw.bat                       start with the defaults (loopback, 7662,
rem                                  the machine data dir; writes
rem                                  %LOCALAPPDATA%\siw\config.json on first run)
rem    siw.bat --config my.json      start from a named config file
rem    siw.bat --port 8000           run-only overrides (the file is not written)
rem ========================================================================

cd /d "%~dp0"

rem -- find a Python 3.11+ (the "py" launcher first, then bare "python") --
set "PY="
where py >nul 2>nul
if %errorlevel%==0 goto try_py
where python >nul 2>nul
if %errorlevel%==0 goto try_python
goto no_python

:try_py
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
  goto run
)
where python >nul 2>nul
if %errorlevel%==0 goto try_python
goto no_python

:try_python
python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
  goto run
)
goto no_python

:no_python
echo siw: no Python 3.11+ found (looked for "py -3" and "python" on PATH).
echo       Install Python 3.11+ from https://www.python.org/downloads/ and retry.
pause
exit /b 2

:run
%PY% -m siw serve %*
endlocal
