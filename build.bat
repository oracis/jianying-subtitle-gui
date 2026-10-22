@echo off
REM ============================================================
REM  剪映字幕粗剪工具 - 一键打包脚本
REM  用法：双击本文件，或在命令行执行 build.bat
REM  产物：dist\JianyingSubtitleGUI\JianyingSubtitleGUI.exe
REM ============================================================
setlocal

cd /d "%~dp0"

echo [1/3] 检查 PyInstaller...
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo    未安装，正在安装...
    pip install pyinstaller
    if errorlevel 1 (
        echo    安装失败，请检查网络或代理
        pause
        exit /b 1
    )
)

echo [2/3] 检查 ffmpeg...
if exist "ffmpeg.exe" (
    echo    使用当前目录的 ffmpeg.exe
) else (
    where ffmpeg >nul 2>nul
    if errorlevel 1 (
        echo    警告：未找到 ffmpeg，打包后程序可能无法解码音频
        echo    建议下载 ffmpeg.exe 放到本目录
    ) else (
        echo    使用 PATH 中的 ffmpeg
    )
)

echo [3/3] 开始打包...
python -m PyInstaller build.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo 打包失败，请查看上面的错误信息
    pause
    exit /b 1
)

echo.
echo ============================================================
echo 打包完成！
echo 程序位置: %cd%\dist\JianyingSubtitleGUI\JianyingSubtitleGUI.exe
echo ============================================================
echo.
echo 提示：
echo   - 模型不包含在程序内，首次转换时会自动下载
echo   - 分发时请复制整个 JianyingSubtitleGUI 文件夹
echo.
pause
