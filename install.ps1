# ==============================================================================
# mail-guard Windows 一键安装与环境部署脚本
# ==============================================================================
$ErrorActionPreference = "Stop"
$DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== 正在部署 mail-guard [Windows 环境] ===" -ForegroundColor Cyan

# 1. 检查 Python 运行环境
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "未检测到 Python 解释器。请先安装 Python 3 并将其加入系统 PATH 环境变量。"
}
$PYTHON_VER = python --version 2>&1
Write-Host "✔ Python 已安装: $PYTHON_VER" -ForegroundColor Green

# 2. 检查核心 CLI 工具依赖
if (Get-Command himalaya -ErrorAction SilentlyContinue) {
    Write-Host "✔ himalaya 已安装" -ForegroundColor Green
} else {
    Write-Host "ℹ himalaya 未检测到。建议使用 'scoop install himalaya' 或从 GitHub Releases 下载。" -ForegroundColor Yellow
}

if (Get-Command goimapnotify -ErrorAction SilentlyContinue) {
    Write-Host "✔ goimapnotify 已安装" -ForegroundColor Green
} else {
    Write-Host "ℹ goimapnotify 未检测到。建议使用 'go install gitlab.com/shackra/goimapnotify@latest' 或下载二进制可执行文件。" -ForegroundColor Yellow
}

# 3. 部署目标配置目录
$CONFIG_DIR = "$HOME\.config\mail-guard"
$HIMALAYA_DIR = "$HOME\.config\himalaya"
$NOTIFY_DIR = "$HOME\.config\goimapnotify"
$BIN_DIR = "$HOME\.local\bin"

New-Item -ItemType Directory -Force -Path $CONFIG_DIR, $HIMALAYA_DIR, $NOTIFY_DIR, $BIN_DIR | Out-Null

if (-not (Test-Path "$CONFIG_DIR\mail-guard.env")) {
    Copy-Item "$DIR\config\mail-guard.env.example" "$CONFIG_DIR\mail-guard.env"
}
if (-not (Test-Path "$HIMALAYA_DIR\config.toml")) {
    Copy-Item "$DIR\config\himalaya.config.toml.example" "$HIMALAYA_DIR\config.toml"
}
if (-not (Test-Path "$NOTIFY_DIR\goimapnotify.yaml")) {
    Copy-Item "$DIR\config\goimapnotify.yaml.example" "$NOTIFY_DIR\goimapnotify.yaml"
}

# 复制 handle_mail.py 确保本地可用
Copy-Item "$DIR\scripts\handle_mail.py" "$CONFIG_DIR\handle_mail.py" -Force

# 4. 创建 Windows 命令行执行入口 (mail-guard.cmd)
$CMD_FILE = "$BIN_DIR\mail-guard.cmd"
$CMD_BODY = "@echo off`r`npython `"$DIR\scripts\cli.py`" %*"
Set-Content -Path $CMD_FILE -Value $CMD_BODY -Encoding ASCII
Write-Host "✔ 统一命令行入口已创建: $CMD_FILE" -ForegroundColor Green
Write-Host "  (请确保 $BIN_DIR 已包含在系统 PATH 中)" -ForegroundColor DarkGray

# 5. 运行端到端测试
Write-Host "`n>> 正在执行跨平台功能自测..." -ForegroundColor Cyan
python "$DIR\scripts\test_pipeline.py"

Write-Host "=== mail-guard Windows 部署成功！===" -ForegroundColor Green
Write-Host "接下来请运行: python `"$DIR\setup_account.py`" 配置你的邮箱账号。"
