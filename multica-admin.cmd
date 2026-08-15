@echo off
setlocal
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0multica_deploy.py" wizard
  exit /b %errorlevel%
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0multica_deploy.py" wizard
  exit /b %errorlevel%
)
echo Python 3.9 or newer was not found. Install Python and enable the PATH option.
exit /b 1
