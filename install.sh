#!/usr/bin/env bash
# ==============================================================================
# mail-guard 一键安装与环境部署脚本
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== 正在部署 mail-guard ==="

# 1. 检查核心 CLI 工具
if ! command -v himalaya >/dev/null 2>&1; then
    echo ">> 正在通过 Homebrew 安装 himalaya..."
    brew install himalaya
else
    echo "✔ himalaya 已安装 ($(himalaya --version | head -n1))"
fi

if ! command -v goimapnotify >/dev/null 2>&1; then
    echo ">> 正在通过 Homebrew 安装 goimapnotify..."
    brew install goimapnotify
else
    echo "✔ goimapnotify 已安装 ($(goimapnotify -version 2>/dev/null || echo 'OK'))"
fi

# 2. 部署目标目录
CONFIG_DIR="$HOME/.config/mail-guard"
HIMALAYA_DIR="$HOME/.config/himalaya"
NOTIFY_DIR="$HOME/.config/goimapnotify"

mkdir -p "$CONFIG_DIR" "$HIMALAYA_DIR" "$NOTIFY_DIR"

# 3. 链接/复制脚本
cp "$DIR/scripts/handle_mail.py" "$CONFIG_DIR/handle_mail.py"
chmod +x "$CONFIG_DIR/handle_mail.py"

if [ ! -f "$CONFIG_DIR/post_shell.sh" ]; then
    cp "$DIR/scripts/post_shell.sh" "$CONFIG_DIR/post_shell.sh"
    chmod +x "$CONFIG_DIR/post_shell.sh"
fi

if [ ! -f "$CONFIG_DIR/mail-guard.env" ]; then
    cp "$DIR/config/mail-guard.env.example" "$CONFIG_DIR/mail-guard.env"
fi

if [ ! -f "$HIMALAYA_DIR/config.toml" ]; then
    cp "$DIR/config/himalaya.config.toml.example" "$HIMALAYA_DIR/config.toml"
fi

if [ ! -f "$NOTIFY_DIR/goimapnotify.yaml" ]; then
    cp "$DIR/config/goimapnotify.yaml.example" "$NOTIFY_DIR/goimapnotify.yaml"
fi

echo "✔ 脚本与配置文件部署完成: $CONFIG_DIR"

# 4. 运行端到端自测
echo ">> 正在执行端到端自测..."
"$DIR/scripts/test_pipeline.sh"

echo "=== mail-guard 部署成功！==="
echo "接下来请运行: python3 $DIR/setup_account.py 配置你的邮箱账号。"
