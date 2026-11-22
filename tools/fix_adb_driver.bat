@echo off
chcp 65001 >nul
title ADB 驱动修复工具
echo.
echo   正在申请管理员权限，请在弹窗里点「是」。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0fix_adb_driver.ps1\"')"
echo   已启动修复窗口，请查看新弹出的窗口。
timeout /t 3 >nul
