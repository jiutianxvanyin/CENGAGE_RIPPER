@echo off
setlocal
rem ---------------------------------------------------------------------------
rem Launch Chrome with a PERSISTENT profile + remote debugging port.
rem
rem Why: Chrome 136+ refuses --remote-debugging-port on the default profile
rem (security fix), so we use a dedicated profile directory. Log in to Cengage
rem ONCE in this window - the session is stored there and reused forever.
rem
rem After starting this, run:  python cengage_ripper.py --attach
rem ---------------------------------------------------------------------------

set "PORT=%~1"
if "%PORT%"=="" set "PORT=9222"

set "PROFILE=%~dp0chrome-profile"
if not exist "%PROFILE%" mkdir "%PROFILE%"

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [!] chrome.exe not found. Edit the CHROME path inside this file.
  pause
  exit /b 1
)

echo Chrome : %CHROME%
echo Profile: %PROFILE%
echo Port   : %PORT%
echo.
echo Log in to Cengage ONCE in the window that opens. It will stay logged in.
echo When the book page is showing, come back here and run:
echo     python cengage_ripper.py --attach %PORT%
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --profile-directory=Default
timeout /t 3 >nul
exit /b 0
