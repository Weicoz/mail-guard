#!/usr/bin/env bash
# ==============================================================================
# mail-guard 后壳脚本 (Post-Shell Hook)
# ==============================================================================
# 作用：大模型或规则提取出验证码/关键信息后，由本脚本负责发送推送或执行操作。
# 参数输入：
#   $1 - 提取到的验证码 (例如 "123456")
#   $2 - 来源平台/服务名 (例如 "GitHub", "Google", "AWS")
#   $3 - 有效时间/说明 (例如 "5分钟内有效", "如非本人操作请忽略")
#   $4 - 邮件主题 (Subject)
#   $5 - 来源邮箱账号名称 (例如 "work", "personal")
# ==============================================================================

CODE="${1:-无}"
SERVICE="${2:-未知服务}"
EXPIRES="${3:-}"
SUBJECT="${4:-验证码通知}"
ACCOUNT="${5:-default}"

# 1. 默认：原生桌面弹窗通知 (自适应 macOS / Linux / Windows)
if command -v osascript >/dev/null 2>&1; then
    # macOS
    osascript -e "display notification \"${SERVICE} 验证码: ${CODE} ${EXPIRES}\" with title \"📧 收到新验证码 [${ACCOUNT}]\" sound name \"Glass\"" 2>/dev/null || true
elif command -v notify-send >/dev/null 2>&1; then
    # Linux (libnotify)
    notify-send -a "mail-guard" "📧 收到新验证码 [${ACCOUNT}]" "${SERVICE} 验证码: ${CODE} ${EXPIRES}" 2>/dev/null || true
elif command -v powershell.exe >/dev/null 2>&1; then
    # Windows (WSL/Git Bash)
    powershell.exe -NoProfile -Command "
        [reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;
        \$notify = New-Object System.Windows.Forms.NotifyIcon;
        \$notify.Icon = [System.Drawing.SystemIcons]::Information;
        \$notify.Visible = \$true;
        \$notify.ShowBalloonTip(5000, '📧 收到新验证码 [${ACCOUNT}]', '${SERVICE} 验证码: ${CODE} ${EXPIRES}', [System.Windows.Forms.ToolTipIcon]::Info);
    " 2>/dev/null || true
fi

# 2. 统一通知调度组件 harness-notify (优先调用，自动按路由分发飞书/Bark/Telegram/ntfy)
HARNESS_NOTIFY_BIN="$(command -v harness-notify 2>/dev/null || echo "${HOME}/.local/bin/harness-notify")"
SENT_VIA_HARNESS=0

if [ -x "$HARNESS_NOTIFY_BIN" ]; then
    MSG_TEXT="收到验证码：${CODE}
说明：${EXPIRES}
来源：${ACCOUNT} - ${SUBJECT}"
    if "$HARNESS_NOTIFY_BIN" send \
        --harness mailguard \
        --session-title "实时验证码 [${SERVICE}]" \
        --message "$MSG_TEXT" >/dev/null 2>&1; then
        SENT_VIA_HARNESS=1
        echo "[mail-guard][post-shell] harness-notify 统一多渠道推送完成: 平台=${SERVICE}, 验证码=${CODE}"
    fi
fi

# 3. 降级备用：直接调用飞书 CLI (lark-cli)
if [ "$SENT_VIA_HARNESS" -eq 0 ]; then
    CONFIG_ENV="${HOME}/.config/mail-guard/mail-guard.env"
    if [ -z "${FEISHU_USER:-}" ] && [ -f "$CONFIG_ENV" ]; then
        FEISHU_USER="$(grep -E '^\s*FEISHU_USER\s*=' "$CONFIG_ENV" | cut -d'=' -f2- | tr -d '"'\'' ' || true)"
    fi

    LARK_BIN="$(command -v lark-cli 2>/dev/null || echo "${HOME}/.local/bin/lark-cli")"
    if [ -n "${FEISHU_USER:-}" ] && [ -x "$LARK_BIN" ]; then
        "$LARK_BIN" im +messages-send --user-id "$FEISHU_USER" --text "📧 【${SERVICE}】收到验证码：${CODE}
说明：${EXPIRES}
来源：${ACCOUNT} - ${SUBJECT}" >/dev/null 2>&1 || true
    fi
fi

# ------------------------------------------------------------------------------
# 选项 A: Bark 推送 (iOS 极力推荐)
# BARK_KEY="YOUR_BARK_KEY"
# if [ -n "$BARK_KEY" ]; then
#     curl -s "https://api.day.app/${BARK_KEY}/${SERVICE}验证码/${CODE}?group=mail-guard&copy=${CODE}" >/dev/null 2>&1
# fi
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# 选项 B: 飞书机器人 Webhook
# FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_UUID"
# if [ -n "$FEISHU_WEBHOOK" ]; then
#     curl -s -X POST "$FEISHU_WEBHOOK" \
#         -H "Content-Type: application/json" \
#         -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"【${SERVICE}】收到验证码：${CODE}\n有效时间：${EXPIRES}\n来源：${ACCOUNT} - ${SUBJECT}\"}}" >/dev/null 2>&1
# fi
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# 选项 C: Telegram Bot 推送
# TG_BOT_TOKEN="YOUR_BOT_TOKEN"
# TG_CHAT_ID="YOUR_CHAT_ID"
# if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
#     curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
#         -d "chat_id=${TG_CHAT_ID}" \
#         -d "text=📧 *[${SERVICE}] 验证码:* \`${CODE}\`%0A说明: ${EXPIRES}" \
#         -d "parse_mode=Markdown" >/dev/null 2>&1
# fi
# ------------------------------------------------------------------------------

echo "[mail-guard][post-shell] 触发完成: 平台=${SERVICE}, 验证码=${CODE}"
