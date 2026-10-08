@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title 抖音续火花 - 多账号源码运行

cd /d "%~dp0"
if errorlevel 1 goto folder_error

set "SPARKKEEPER_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%SPARKKEEPER_PYTHON%" goto python_ready
set "SPARKKEEPER_PYTHON=%~dp0venv\Scripts\python.exe"
if exist "%SPARKKEEPER_PYTHON%" goto python_ready
set "SPARKKEEPER_PYTHON=python"
python --version >nul 2>&1
if errorlevel 1 goto python_error
:python_ready
if not exist "%~dp0app.py" goto app_error

set "PYTHONUTF8=1"
set "ENV_FILE_PATH=%~dp0.env"
set "HOST=127.0.0.1"
set "PORT=8000"
set "SPARKKEEPER_MULTI_ACCOUNT=1"
set "SPARKKEEPER_AUTO_RUN=0"
set "SPARKKEEPER_BACKUP_ENABLED=0"

echo ==========================================================
echo  抖音续火花 - 多账号版源码启动
echo ==========================================================
echo.
echo [设置] 仅本机访问，定时发送与自动备份已关闭。
echo [地址] http://127.0.0.1:8000/static/multi.html
echo [说明] 等待服务启动后，在浏览器打开上面的地址。
echo [说明] 运行期间保留此窗口，按 Ctrl+C 停止服务。
echo.

"%SPARKKEEPER_PYTHON%" "%~dp0app.py"
set "SPARKKEEPER_EXIT=%ERRORLEVEL%"
echo.
if "%SPARKKEEPER_EXIT%"=="0" (
    echo [已停止] 服务已退出。
) else (
    echo [启动或运行失败] 退出码：%SPARKKEEPER_EXIT%
    echo 请查看上面的错误信息；如果服务已经运行，请直接打开控制台。
)
echo.
pause
exit /b %SPARKKEEPER_EXIT%

:folder_error
echo [错误] 无法进入启动文件所在目录。
goto fail

:python_error
echo [错误] 未找到已配置的 Python 环境：
echo "%SPARKKEEPER_PYTHON%"
echo 请先按 README 在本版本目录创建 .venv 并安装 requirements.txt。
goto fail

:app_error
echo [错误] 当前文件夹缺少 app.py，请将启动文件放在多账号版根目录。
goto fail

:fail
echo.
pause
exit /b 1
