@echo off
REM EQ Forge 2.0 - start the local server and open the app in your browser.
REM   run.bat        -> this PC only (default)
REM   run.bat --lan  -> also reachable from your phone/tablet on the same wifi
REM                     (no password - only do this on your own network)
REM
REM The browser is opened by serve.py (--open), NOT with `start` from here: on a
REM first run the server downloads the ~8MB item database before it binds, and a
REM browser launched from this file would land on a dead page for 5-30 seconds.
cd /d "%~dp0"
python serve.py --open %*
if errorlevel 1 (
  echo.
  echo Could not start. If Windows says 'python' is not recognized, reinstall
  echo Python 3 from https://www.python.org/downloads/ and TICK the
  echo "Add python.exe to PATH" checkbox in the installer.
)
pause
