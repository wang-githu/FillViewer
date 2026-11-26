@echo off
rem 需要管理员权限：右键 → 以管理员身份运行
rem 用相对路径安装：本脚本所在目录的上一层即 FillViewer 根目录
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "EXE=%ROOT%\FillViewer.exe"
if not exist "%EXE%" (
  echo [ERROR] 找不到 %EXE%
  echo         请先在本目录运行一次 server.py 或用配置台生成 FillViewer.exe
  pause
  exit /b 1
)
schtasks /create /tn "FillViewer服务" /tr ""%EXE%" -nobrowser" /sc onstart /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (echo 创建失败：请确认以管理员身份运行) else (echo 已创建计划任务 FillViewer服务 → %EXE%)
pause
