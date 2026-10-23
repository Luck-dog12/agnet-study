@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    >&2 echo Startup failed: project virtual environment not found.
    exit /b 2
)

pushd "%PROJECT_DIR%" >nul
"%PYTHON_EXE%" -m app.main %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
