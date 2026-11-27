@echo off
setlocal EnableExtensions
rem ========================================================================
rem  siw-test.bat - run the SIW test battery
rem
rem  The battery: the lifted kernel, the console wire shape, the download
rem  queue. No network, no kiwix-serve, nothing outside the tree.
rem
rem    siw-test.bat                  run everything
rem    siw-test.bat -k console       pass-through to pytest (any pytest flag)
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
  goto run_tests
)
where python >nul 2>nul
if %errorlevel%==0 goto try_python
goto no_python

:try_python
python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
  goto check_pytest
)
goto no_python

:no_python
echo siw-test: no Python 3.11+ found (looked for "py -3" and "python" on PATH).
echo       Install Python 3.11+ from https://www.python.org/downloads/ and retry.
pause
exit /b 2

:check_pytest
rem -- pytest is the only outside need; offer to install it once --
%PY% -m pytest --version >nul 2>nul
if not errorlevel 1 goto run_tests
echo siw-test: pytest not found - installing it for this user...
%PY% -m pip install --quiet --user pytest
if errorlevel 1 (
  echo siw-test: the automatic install failed. Run it by hand:
  echo           %PY% -m pip install pytest
  pause
  exit /b 2
)

:run_tests
%PY% -m pytest tests/ %*
endlocal
