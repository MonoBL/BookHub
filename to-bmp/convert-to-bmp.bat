@echo off
REM Double-click to convert JPEGs in this folder to 8-bit grayscale BMP
REM for the XTeink X4. Only converts JPEGs that don't already have a .bmp.
cd /d "%~dp0"
python "%~dp0convert-to-bmp.py"
pause
