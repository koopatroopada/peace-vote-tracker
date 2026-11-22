@echo off
chcp 65001 >nul
title 打包 VoteTracker.exe

echo.
echo   正在打包单文件 exe...
echo.

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --console ^
  --name VoteTracker ^
  --add-data "dashboard.html;." ^
  --collect-all rapidocr_onnxruntime ^
  --collect-all onnxruntime ^
  --collect-data PIL ^
  --hidden-import PIL._tkinter_finder ^
  app.py

if errorlevel 1 (
    echo.
    echo   打包失败，看上面报错。
    pause
    exit /b 1
)

echo.
echo   完成：dist\VoteTracker.exe
echo.
dir dist
pause
