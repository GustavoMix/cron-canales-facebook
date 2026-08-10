@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 goto error
py -m playwright install chromium
if errorlevel 1 goto error
echo INSTALACION TERMINADA
pause
exit /b 0
:error
echo ERROR. Verifica Python y el comando py.
pause
exit /b 1
