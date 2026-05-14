@echo off
REM 切到本文件所在目录的上一级，这样可以用 python -m XWJJJ260511 方式启动包。
cd /d "%~dp0\.."
REM 把用户传给 run.bat 的所有参数继续转交给 Python 程序。
python -m XWJJJ260511 %*
