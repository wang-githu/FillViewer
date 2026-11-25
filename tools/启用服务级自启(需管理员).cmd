@echo off
rem 需要管理员权限：右键 → 以管理员身份运行
schtasks /create /tn "FillViewer服务" /tr ""C:\FillViewer-OSS\FillViewer.exe" -nobrowser" /sc onstart /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (echo 创建失败：请确认已用管理员运行) else (echo 已创建开机任务：FillViewer服务)
pause
