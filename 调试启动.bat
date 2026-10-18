@echo off
cd /d "%~dp0"
echo 正在用控制台方式启动，请观察窗口标题下方是否有黄色提示条...
python BiliCacheMerge.py
echo.
pause
