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

# 1. 默认：macOS 桌面原生弹窗通知 (保证开箱即可感知)
osascript -e "display notification \"${SERVICE} 验证码: ${CODE} ${EXPIRES}\" with title \"📧 收到新验证码 [${ACCOUNT}]\" sound name \"Glass\"" 2>/dev/null || true

# 2. 用户自定义配置通道 (按需取消注释并填入你的 Key/Webhook)

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
