@echo off
rem ---------------------------------------------------------------------------
rem  Keep this file pure ASCII. cmd.exe tracks its position in a batch file by
rem  BYTE offset, and under `chcp 65001` a multi-byte character makes it resume
rem  two bytes late on the following lines -- "echo" is read as "ho" and every
rem  command after it fails with "is not recognized". This file used to contain
rem  em dashes and did exactly that on a machine where it had never been run.
rem  A test in tests/test_launchers.py enforces it.
rem ---------------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
title FPV FM emulator - setup
echo ============================================================
echo   FPV FM emulator - setup (run once)
echo ============================================================
echo.

rem `python` is not the only way in: the python.org installer always provides the
rem `py` launcher, and it works even when "Add to PATH" was left unticked - which
rem is the usual reason this script cannot find an interpreter that is installed.
set "PY_CMD="
where python >nul 2>nul && set "PY_CMD=python"
if not defined PY_CMD where py >nul 2>nul && set "PY_CMD=py -3"

if not defined PY_CMD (
    echo [!] No Python found.
    echo.
    echo     Neither "python" nor the "py" launcher is available here.
    echo     Install Python from python.org and tick "Add python.exe to PATH"
    echo     in the FIRST installer window.
    echo.
    echo     Python 3.11 or 3.12 is the safe choice. A version released in the
    echo     last few months often has no numpy or PySide6 packages yet, and the
    echo     install below would fail.
    echo.
    pause
    exit /b 1
)
echo [i] Using: %PY_CMD%
%PY_CMD% --version

rem A .venv that arrived with the folder from another machine is worse than none:
rem its binaries are compiled for one exact Python and it records the path of the
rem interpreter that built it. `python -m venv` leaves an existing directory
rem alone, so the mismatch would survive this script and running setup again -
rem the first thing anyone tries - would change nothing.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import numpy" >nul 2>nul
    if errorlevel 1 (
        echo [i] The existing .venv does not work with its Python - rebuilding it.
        echo     ^(That is what a .venv copied from another computer looks like.^)
        rmdir /s /q .venv
    )
)

echo [1/4] Creating the .venv environment ...
%PY_CMD% -m venv .venv
if errorlevel 1 (
    echo [!] Failed to create .venv
    pause
    exit /b 1
)

echo [2/4] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/4] Installing the core dependencies (numpy, PyYAML, PySide6) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] Dependency installation FAILED - see the errors above.
    echo     Nothing was installed: pip aborts the whole requirements file when a
    echo     single entry cannot be resolved. The GUI will not start like this.
    echo     The usual cause is a Python too new to have numpy/PySide6 packages -
    echo     install 3.11 or 3.12 and run setup.bat again.
    echo.
    pause
    exit /b 1
)

echo [4/4] Installing the hardware dependencies (pyadi-iio, pylibiio) ...
".venv\Scripts\python.exe" -m pip install -r requirements-hw.txt
if errorlevel 1 (
    echo.
    echo [i] The hardware dependencies were NOT installed - this is NOT fatal.
    echo     The GUI, the CLI and the file/null backends work without them;
    echo     only real transmission through Pluto+ needs pyadi-iio + pylibiio.
    echo.
)

echo.
echo ============================================================
echo   Done. Start the GUI by double-clicking run_gui.bat
echo ============================================================
echo.
echo   HackRF One: run once  .venv\Scripts\python.exe scripts\fetch_hackrf.py
echo   (SoapySDR for Lime/BladeRF/USRP - separately, see requirements-hw.txt)
echo.
pause
