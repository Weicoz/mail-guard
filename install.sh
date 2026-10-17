#!/usr/bin/env bash
# ==============================================================================
# mail-guard 一键安装与环境部署脚本 (macOS & Linux)
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_TYPE="$(uname -s)"
echo "=== 正在部署 mail-guard [操作系统: ${OS_TYPE}] ==="

# 1. 检查核心 CLI 工具依赖 (himalaya 与 goimapnotify)
case "$OS_TYPE" in
    Darwin)
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
            echo "✔ goimapnotify 已安装"
        fi
        ;;
    Linux)
        if ! command -v himalaya >/dev/null 2>&1; then
            echo ">> 正在检测 Linux 安装渠道..."
            if command -v cargo >/dev/null 2>&1; then
                cargo install himalaya
            elif command -v pacman >/dev/null 2>&1; then
                sudo pacman -S --noconfirm himalaya || true
            else
                echo "ℹ 未检测到 himalaya。请从 https://github.com/soywod/himalaya/releases 下载或使用 cargo/包管理器安装。"
            fi
        else
            echo "✔ himalaya 已安装 ($(himalaya --version | head -n1))"
        fi

        if ! command -v goimapnotify >/dev/null 2>&1; then
            if command -v go >/dev/null 2>&1; then
                echo ">> 正在通过 go install 安装 goimapnotify..."
                go install gitlab.com/shackra/goimapnotify@latest
            else
                echo "ℹ 未检测到 goimapnotify。请从 https://gitlab.com/shackra/goimapnotify 下载或通过 go install 安装。"
            fi
        else
            echo "✔ goimapnotify 已安装"
        fi
        ;;
    *)
        echo "ℹ 其它系统，请确保已安装 himalaya 与 goimapnotify。"
        ;;
esac

# 2. 部署目标目录
CONFIG_DIR="$HOME/.config/mail-guard"
HIMALAYA_DIR="$HOME/.config/himalaya"
NOTIFY_DIR="$HOME/.config/goimapnotify"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$CONFIG_DIR" "$HIMALAYA_DIR" "$NOTIFY_DIR" "$BIN_DIR"

# 3. 链接脚本与统一 CLI 入口
ln -sf "$DIR/scripts/handle_mail.py" "$CONFIG_DIR/handle_mail.py"
chmod +x "$DIR/scripts/handle_mail.py"

chmod +x "$DIR/scripts/cli.py"
ln -sf "$DIR/scripts/cli.py" "$BIN_DIR/mail-guard"
ln -sf "$DIR/scripts/cli.py" "$CONFIG_DIR/mail-guard"
echo "✔ 统一命令行工具已安装至: $BIN_DIR/mail-guard"

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

# 4. 部署守护服务 (macOS LaunchAgents / Linux systemd)
PYTHON_BIN="$(which python3 || echo "/usr/bin/python3")"

if [ "$OS_TYPE" = "Darwin" ]; then
    LAUNCH_DIR="$HOME/Library/LaunchAgents"
    if [ -d "$LAUNCH_DIR" ]; then
        echo ">> 正在配置 macOS 开机自启 LaunchAgents..."
        if [ -f "$DIR/launchd/com.weicoz.mailguard.plist.example" ]; then
            sed -e "s|__PROJECT_DIR__|$DIR|g" \
                -e "s|__HOME__|$HOME|g" \
                -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
                "$DIR/launchd/com.weicoz.mailguard.plist.example" > "$LAUNCH_DIR/com.weicoz.mailguard.plist"
        fi
        if [ -f "$DIR/launchd/com.weicoz.mailguard.summary.plist.example" ]; then
            sed -e "s|__PROJECT_DIR__|$DIR|g" \
                -e "s|__HOME__|$HOME|g" \
                -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
                "$DIR/launchd/com.weicoz.mailguard.summary.plist.example" > "$LAUNCH_DIR/com.weicoz.mailguard.summary.plist"
        fi
        # 根据 mail-guard.env 中的 SUMMARY_SCHEDULE 动态应用排班配置
        "$PYTHON_BIN" "$DIR/scripts/cli.py" daemon schedule --apply >/dev/null 2>&1 || true
        echo "✔ LaunchAgents 守护配置已生成至: $LAUNCH_DIR"
    fi
elif [ "$OS_TYPE" = "Linux" ]; then
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_USER_DIR"
    echo ">> 正在配置 Linux systemd 用户服务..."
    if [ -f "$DIR/systemd/mailguard.service.example" ]; then
        sed -e "s|__PROJECT_DIR__|$DIR|g" \
            -e "s|__HOME__|$HOME|g" \
            -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
            "$DIR/systemd/mailguard.service.example" > "$SYSTEMD_USER_DIR/mailguard.service"
    fi
    if [ -f "$DIR/systemd/mailguard-summary.service.example" ]; then
        sed -e "s|__PROJECT_DIR__|$DIR|g" \
            -e "s|__HOME__|$HOME|g" \
            -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
            "$DIR/systemd/mailguard-summary.service.example" > "$SYSTEMD_USER_DIR/mailguard-summary.service"
    fi
    if [ -f "$DIR/systemd/mailguard-summary.timer.example" ]; then
        cp "$DIR/systemd/mailguard-summary.timer.example" "$SYSTEMD_USER_DIR/mailguard-summary.timer"
    fi
    "$PYTHON_BIN" "$DIR/scripts/cli.py" daemon schedule --apply >/dev/null 2>&1 || true
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload || true
        echo "✔ systemd 用户服务已加载 (启用命令: systemctl --user enable --now mailguard.service mailguard-summary.timer)"
    fi
fi

# 5. 运行端到端自测
echo ">> 正在执行端到端自测..."
"$DIR/scripts/test_pipeline.sh"

echo "=== mail-guard 部署成功！==="
echo "接下来请运行: python3 $DIR/setup_account.py 配置你的邮箱账号。"
