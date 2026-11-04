@echo off
rem  Rebuilds "Photo Sequencer.exe" from app.py.
rem  You only need this if you change the code - the built .exe already works.

cd /d "%~dp0"
title Building Photo Sequencer

echo.
echo   Building. This takes a couple of minutes.
echo.

python -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller
python make_icon.py

rem  --onedir, not --onefile: a single-file build has to unpack ~60 MB to a
rem  temp folder on every launch, which took over a minute. This starts in
rem  about a second because the files are already on disk.
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "Photo Sequencer" ^
  --icon icon.ico ^
  --add-data "icon.ico;." ^
  --add-data "fonts;fonts" ^
  --add-data "check-off.png;." ^
  --add-data "check-on.png;." ^
  --add-data "radio-off.png;." ^
  --add-data "radio-on.png;." ^
  --hidden-import piexif ^
  --exclude-module tkinter ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --noupx ^
  app.py

echo.
if exist "dist\Photo Sequencer\Photo Sequencer.exe" (
  echo   Done:  dist\Photo Sequencer\Photo Sequencer.exe
) else (
  echo   The build did not produce an .exe - scroll up for the error.
)
echo.
pause
