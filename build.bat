@echo off
echo.
echo === SwitchBot Scheduler Build Script ===
echo.

echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo.
echo [2/3] Cleaning old build...
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist SwitchBotScheduler.spec del /q SwitchBotScheduler.spec

echo.
echo [3/3] Building exe (takes 1-2 minutes)...
for /f "delims=" %%I in ('python -c "import sys; print(sys.base_prefix)"') do set "PYBASE=%%I"
python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --name SwitchBotScheduler ^
  --additional-hooks-dir hooks ^
  --hidden-import tkinter ^
  --add-data "%PYBASE%\tcl\tcl8.6;_tcl_data" ^
  --add-data "%PYBASE%\tcl\tk8.6;_tk_data" ^
  --add-data "%PYBASE%\tcl\tcl8;tcl8" ^
  --add-binary "%PYBASE%\DLLs\tcl86t.dll;." ^
  --add-binary "%PYBASE%\DLLs\tk86t.dll;." ^
  --collect-all switchbot ^
  --collect-all bleak ^
  --collect-all winrt ^
  --collect-all pystray ^
  app.py
if errorlevel 1 goto :err

echo.
echo ============================================
echo Build OK!
echo Output: %cd%\dist\SwitchBotScheduler.exe
echo.
echo Ship these two files to the user:
echo   1. dist\SwitchBotScheduler.exe
echo   2. shi-yong-shuo-ming.txt  (the Chinese manual)
echo ============================================
echo.
pause
exit /b 0

:err
echo.
echo [ERROR] Build failed
pause
exit /b 1
