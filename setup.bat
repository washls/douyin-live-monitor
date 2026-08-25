@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   抖音直播监听器 - 环境安装脚本
echo ========================================
echo.

:: Check Python
echo [1/2] 检查 Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo    Python ✓

:: Install Python dependencies
echo.
echo [2/2] 安装 Python 依赖...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo    依赖安装完成 ✓

echo.
echo ========================================
echo   安装完成!
echo.
echo   下一步:
echo   1. 运行: python monitor.py
echo      首次运行会提示你配置 Server酱³ 推送和选择监控主播
echo   2. (可选) 运行: python monitor.py --test
echo      测试 Server酱³ 连接是否正常
echo   3. (可选) 运行: python monitor.py --once
echo      执行一次检测，确认目标博主信息正确
echo   4. 运行: python monitor.py
echo      开始监控全部已启用主播!
echo   5. (可选) 运行: python monitor.py --add-streamer URL
echo      添加更多主播
echo ========================================
pause
