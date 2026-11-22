@echo off
chcp 65001 >nul
title 打包 VoteTracker.exe
setlocal

echo.
echo   [1/3] 准备内置资源...
if not exist "build_assets\platform-tools\adb.exe" (
    echo         未找到 adb，尝试从系统路径或 Android SDK 复制
    if not exist "build_assets\platform-tools" mkdir "build_assets\platform-tools"
    for /f "delims=" %%i in ('where adb 2^>nul') do (
        if not exist "build_assets\platform-tools\adb.exe" (
            copy /y "%%~dpi*.*" "build_assets\platform-tools\" >nul
            echo         已从 %%~dpi 复制
        )
    )
    if not exist "build_assets\platform-tools\adb.exe" (
        if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
            copy /y "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" "build_assets\platform-tools\" >nul
            copy /y "%LOCALAPPDATA%\Android\Sdk\platform-tools\AdbWinApi.dll" "build_assets\platform-tools\" >nul
            copy /y "%LOCALAPPDATA%\Android\Sdk\platform-tools\AdbWinUsbApi.dll" "build_assets\platform-tools\" >nul
            echo         已从 Android SDK 复制
        )
    )
)
if not exist "build_assets\platform-tools\adb.exe" (
    echo.
    echo   警告：没有 adb，打出来的 exe 需要用户自己装 Platform-Tools。
    echo.
)

if not exist "build_assets\tools" mkdir "build_assets\tools"
copy /y "tools\fix_adb_driver.bat" "build_assets\tools\" >nul
copy /y "tools\fix_adb_driver.ps1" "build_assets\tools\" >nul

echo   [2/3] 打包单文件 exe...
echo.
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --console ^
  --name VoteTracker ^
  --add-data "dashboard.html;." ^
  --add-data "build_assets\platform-tools;platform-tools" ^
  --add-data "build_assets\tools;tools" ^
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
echo   [3/3] 完成：dist\VoteTracker.exe
echo.
dir dist
pause
