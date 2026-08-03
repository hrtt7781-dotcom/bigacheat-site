@echo off
rem Biga Cheat Loader - PyInstaller ile Loader.exe derler
rem Gereksinim: pip install pyinstaller

cd /d "%~dp0"

pyinstaller --onefile --noconsole --name "BigaCheat-Loader" --clean loader.py

echo.
echo Derleme tamamlandi: dist\BigaCheat-Loader.exe
pause
