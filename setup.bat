@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   抖音直播监听器 - 环境安装脚本
echo ========================================
echo.

:: Check Python
echo [1/3] 检查 Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo    Python ✓

:: Check Node.js (optional, for signature generation)
echo.
echo [2/3] 检查 Node.js (可选，用于签名生成)...
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 未找到 Node.js (非必需，但建议安装)
    echo   下载地址: https://nodejs.org/
    echo   没有 Node.js 将使用 HTML 页面解析方式检测直播
) else (
    node --version
    echo    Node.js ✓
)

:: Install Python dependencies
echo.
echo [3/3] 安装 Python 依赖...
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
echo      开始持续监控!
echo ========================================
pause
