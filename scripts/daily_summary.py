#!/usr/bin/env python3
"""
mail-guard: 每日跨邮箱重要邮件与待办智能汇总工具 (daily_summary.py)

核心特性:
1. 每天定时 (如 4 次: 08:30, 12:30, 18:00, 22:00) 自动扫描全量邮箱。
2. 状态无损: 严格采用 IMAP 只读 PEEK 模式，绝不修改邮件的已读/未读状态。
3. 纯文本物理隔离: 绝不请求外部图片、CSS 或远程外链，物理杜绝 IP 泄露与阅读追踪。
4. 智能提炼: 接入 DeepSeek 大模型 (支持本地规则无缝回退)，识别待办动作与重要告警。
5. 多渠道触达: 自动触发 Bark 手机推送、macOS 桌面弹窗并归档本地 Markdown 报告。
"""

import sys
import os
import re
import json
import socket
import struct
import argparse
import imaplib
import ssl
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.error

# 路径常量
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
ACCOUNTS_CONFIG = os.path.join(CONFIG_DIR, "accounts.json")
STATE_FILE = os.path.join(CONFIG_DIR, "summary_state.json")
SUMMARIES_DIR = os.path.join(CONFIG_DIR, "summaries")
ENV_FILE = os.path.join(CONFIG_DIR, "mail-guard.env")

# 引入项目自带模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import ms_oauth2
except ImportError:
    ms_oauth2 = None

try:
    from llm_adapter import call_llm_or_harness
except ImportError:
    call_llm_or_harness = None

def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line_s = line.strip()
                if line_s and not line_s.startswith("#") and "=" in line_s:
                    k, v = line_s.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()
# Bark 设备配置 (优先从 mail-guard.env 或环境变量读取)
BARK_KEY = ENV.get("BARK_KEY") or os.environ.get("BARK_KEY", "")
BARK_SERVER = ENV.get("BARK_SERVER", os.environ.get("BARK_SERVER", "https://api.day.app"))
FEISHU_USER = ENV.get("FEISHU_USER") or os.environ.get("FEISHU_USER", "")

# 优先从 mail-guard.env 读取 LLM 配置，回退到环境变量
LLM_API_KEY = ENV.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = ENV.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = ENV.get("LLM_MODEL", "deepseek-chat")

def decode_mime_header(val):
    if not val:
        return ""
    try:
        parts = decode_header(val)
        res = []
        for text, enc in parts:
            if isinstance(text, bytes):
                res.append(text.decode(enc or 'utf-8', errors='ignore'))
            else:
                res.append(str(text))
        return "".join(res)
    except Exception:
        return str(val)

def extract_plain_text(msg):
    """仅提取 text/plain 纯文本，杜绝外部资源加载"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get('Content-Disposition'))
            if ctype == 'text/plain' and 'attachment' not in cdisp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                if payload:
                    body += payload.decode(charset, errors='ignore')
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        body = payload.decode(charset, errors='ignore') if payload else ""
    return body.strip()

def load_accounts():
    if not os.path.exists(ACCOUNTS_CONFIG):
        raise FileNotFoundError(f"配置文件未找到: {ACCOUNTS_CONFIG}")
    with open(ACCOUNTS_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": None, "watermarks": {}}

def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def connect_imap(acc_name, acc_cfg):
    host = acc_cfg.get("host")
    port = int(acc_cfg.get("port", 993))
    user = acc_cfg.get("user")
    pwd = acc_cfg.get("pass", "")
    proxy = acc_cfg.get("proxy")
    auth_type = acc_cfg.get("auth_type", "password")

    ctx = ssl.create_default_context()

    if proxy:
        proxy_host, proxy_port = proxy.split(":")
        proxy_port = int(proxy_port)
        s = socket.create_connection((proxy_host, proxy_port), timeout=10)
        s.sendall(b"\x05\x01\x00")
        auth = s.recv(2)
        if auth != b"\x05\x00":
            raise RuntimeError(f"[{acc_name}] SOCKS5 代理认证失败: {auth}")

        host_bytes = host.encode("utf-8")
        req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port)
        s.sendall(req)
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            raise RuntimeError(f"[{acc_name}] SOCKS5 代理连接目标失败")

        sslsock = ctx.wrap_socket(s, server_hostname=host)

        class ProxiedIMAP(imaplib.IMAP4_SSL):
            def _create_socket(self, timeout):
                return sslsock

        mail = ProxiedIMAP(host, port, ssl_context=ctx)
    else:
        mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)

    if auth_type == "xoauth2":
        if not ms_oauth2:
            raise RuntimeError("ms_oauth2 模块不可用")
        token = ms_oauth2.get_valid_access_token()
        if not token:
            raise RuntimeError(f"获取 {user} 的 OAuth2 Access Token 失败")
        auth_string = f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
        mail.authenticate("XOAUTH2", lambda x: auth_string)
    else:
        mail.login(user, pwd)

    return mail

def fetch_recent_emails(acc_name, acc_cfg, hours=8, last_uid=None):
    """抓取最近 N 小时或上次扫描以来的邮件 (只读 PEEK 模式)"""
    mails = []
    try:
        mail = connect_imap(acc_name, acc_cfg)
    except Exception as e:
        print(f"[{acc_name}] IMAP 连接失败: {e}", file=sys.stderr)
        return mails, last_uid

    try:
        mail.select("INBOX", readonly=True)

        since_date = (datetime.now() - timedelta(hours=hours + 24)).strftime("%d-%b-%Y")
        res, data = mail.uid("search", None, "SINCE", since_date)
        if res != "OK" or not data[0]:
            mail.logout()
            return mails, last_uid

        uids = data[0].split()
        if not uids:
            mail.logout()
            return mails, last_uid

        new_max_uid = int(uids[-1])
        # 仅扫描比上次记录更新的 UID (如果有的话)
        candidate_uids = [u for u in uids if (last_uid is None or int(u) > last_uid)]
        # 限制单次最多扫描最近 30 封，防止大模型上下文溢出
        if len(candidate_uids) > 30:
            candidate_uids = candidate_uids[-30:]

        for uid in candidate_uids:
            res, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
            if res != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue

            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", ""))
            from_addr = decode_mime_header(msg.get("From", ""))
            date_str = decode_mime_header(msg.get("Date", ""))
            body = extract_plain_text(msg)

            mails.append({
                "account": acc_name,
                "uid": uid.decode("utf-8", errors="ignore"),
                "date": date_str,
                "from": from_addr,
                "subject": subject,
                "body_preview": body[:800]
            })

        mail.logout()
        return mails, new_max_uid
    except Exception as e:
        print(f"[{acc_name}] 抓取邮件异常: {e}", file=sys.stderr)
        return mails, last_uid

def is_noise_email(subject: str, from_addr: str, body: str) -> bool:
    """本地粗筛快速排除明显的广告/垃圾推广"""
    text = f"{subject} {from_addr} {body}".lower()
    noise_keywords = [
        "unsubscribe", "退订", "退订邮件", "免责声明", "优惠券", "促销",
        "sale", "discount", "newsletter", "特惠", "双11", "广告", "会员日"
    ]
    # 如果明确带账单、安全、验证码，不视为噪音
    important_keywords = ["安全", "账单", "验证码", "alert", "security", "bill", "invoice", "payment", "due", "warning", "action required"]
    if any(k in text for k in important_keywords):
        return False
    return any(k in text for k in noise_keywords)

def analyze_with_llm(all_mails):
    """使用大模型或本地 Harness 做深度语义分析，提炼待办与关键告警"""
    # 预过滤噪音邮件
    candidates = [m for m in all_mails if not is_noise_email(m["subject"], m["from"], m["body_preview"])]
    if not candidates:
        return {"action_items": [], "important_alerts": [], "summary_text": "本周期内未检测到需要处理的待办或重要提醒。"}

    prompt = f"""你是一个高级私人邮件行政助理。请审阅以下最近收到的邮件列表，精准识别出：
1. 【待办事项 (Action Items)】：需要我本人操作、确认、回复、支付、签署或处理的事项（例如账单待付、审批、电子合同、会议邀请需回复、服务即将过期停运等）。
2. 【重要提醒 (Important Alerts)】：关键系统通知、安全告警、异地登录、密码重置、重大账户变动等。
3. 自动忽略并过滤任何非紧急的日常推广、普通周报、营销广告或纯资讯。

待分析邮件列表:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

请严格返回合法的 JSON 对象，不要输出 Markdown 代码块，格式严格如下:
{{
  "action_items": [
    {{
      "account": "邮箱名",
      "from": "发件人",
      "subject": "主题",
      "action_summary": "具体需要我做什么，有何截止时间或限制",
      "urgency": "高/中"
    }}
  ],
  "important_alerts": [
    {{
      "account": "邮箱名",
      "from": "发件人",
      "subject": "主题",
      "alert_summary": "核心提醒事项与影响"
    }}
  ],
  "summary_text": "用 1-3 句话概括本周期的核心动态（如果没有特别事项则明确说明良好）"
}}
"""

    if call_llm_or_harness:
        try:
            data = call_llm_or_harness(prompt, expect_json=True, env=ENV, timeout=60)
            if data and isinstance(data, dict):
                data.setdefault("action_items", [])
                data.setdefault("important_alerts", [])
                data.setdefault("summary_text", "")
                return data
            print("[daily-summary] 大模型/Harness 未返回有效结构化摘要，回退到本地规则分析", file=sys.stderr)
        except Exception as e:
            print(f"[daily-summary] 模型/Harness 调用异常 ({e})，回退到本地规则分析", file=sys.stderr)

    return analyze_with_rules(all_mails)

def analyze_with_rules(all_mails):
    """大模型不可用时的本地规则引擎回退"""
    action_items = []
    important_alerts = []

    action_keywords = ["待支付", "到期", "续费", "请处理", "请回复", "签署", "待确认", "action required", "payment due", "renew", "confirm"]
    alert_keywords = ["安全警告", "异常登录", "密码重置", "风险", "冻结", "security alert", "suspicious", "unauthorized", "failed login", "password reset"]

    for m in all_mails:
        text = f"{m['subject']} {m['body_preview']}".lower()
        if any(k in text for k in action_keywords):
            action_items.append({
                "account": m["account"],
                "from": m["from"],
                "subject": m["subject"],
                "action_summary": f"匹配到待办关键词，请及时核查邮件正文",
                "urgency": "高" if "紧急" in text or "到期" in text else "中"
            })
        elif any(k in text for k in alert_keywords):
            important_alerts.append({
                "account": m["account"],
                "from": m["from"],
                "subject": m["subject"],
                "alert_summary": f"匹配到安全/风险告警通知"
            })

    summary_text = f"本地规则扫描完成，发现 {len(action_items)} 项待办，{len(important_alerts)} 项重要提醒。"
    return {
        "action_items": action_items,
        "important_alerts": important_alerts,
        "summary_text": summary_text
    }

def send_harness_notify(session_title: str, message: str) -> bool:
    """使用本机统一通知组件 harness-notify 派发消息"""
    import shutil, subprocess
    harness_bin = shutil.which("harness-notify") or os.path.expanduser("~/.local/bin/harness-notify")
    if not (os.path.exists(harness_bin) and os.access(harness_bin, os.X_OK)):
        return False

    try:
        proc = subprocess.run(
            [harness_bin, "send", "--harness", "mailguard", "--title", session_title, "--body", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15
        )
        if proc.returncode == 0:
            print(f"[daily-summary] harness-notify 统一多渠道推送成功: {session_title}")
            return True
        else:
            err = proc.stderr.decode(errors="ignore").strip()
            print(f"[daily-summary] harness-notify 推送返回错误: {err}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[daily-summary] harness-notify 调用异常: {e}", file=sys.stderr)
        return False

def send_notifications(analysis, report_file=None):
    """推送通知到桌面与手机/飞书 (优先通过 harness-notify 统一组件派发)"""
    actions_count = len(analysis.get("action_items", []))
    alerts_count = len(analysis.get("important_alerts", []))
    total_important = actions_count + alerts_count

    now_str = datetime.now().strftime("%H:%M")

    if total_important == 0:
        session_title = f"定巡简报 [{now_str}]"
        desktop_title = f"🟢 邮箱定巡简报 [{now_str}]"
        desktop_body = "所有邮箱运行正常，无待处理事项或重要告警。"
    else:
        session_title = f"每日汇总 [{now_str}] ({actions_count}待办, {alerts_count}提醒)"
        desktop_title = f"📬 邮箱每日汇总 [{now_str}]: {actions_count} 项待办, {alerts_count} 项提醒"
        lines = []
        for a in analysis.get("action_items", [])[:2]:
            lines.append(f"• [待办] {a['subject'][:22]}")
        for al in analysis.get("important_alerts", [])[:2]:
            lines.append(f"• [提醒] {al['subject'][:22]}")
        desktop_body = "\n".join(lines)

    # 1. 发送原生桌面弹窗通知 (自适应 macOS / Linux / Windows)
    send_desktop_notification(desktop_title, desktop_body)

    # 2. 构建结构化推送摘要
    msg_lines = [analysis.get("summary_text", "")]
    actions = analysis.get("action_items", [])
    alerts = analysis.get("important_alerts", [])
    if actions:
        msg_lines.append(f"\n🚨 待处理事项 ({len(actions)}):")
        for a in actions:
            msg_lines.append(f"• [{a['account'].upper()}] {a['subject']} -> {a['action_summary']}")
    if alerts:
        msg_lines.append(f"\n⚠️ 重要提醒 ({len(alerts)}):")
        for al in alerts:
            msg_lines.append(f"• [{al['account'].upper()}] {al['subject']} -> {al['alert_summary']}")
    push_message = "\n".join(msg_lines).strip()

    # 3. 优先使用 harness-notify 统一派发通道
    sent_via_harness = send_harness_notify(session_title, push_message)

    # 4. 若 harness-notify 未安装或派发失败，自动降级为独立渠道直发
    if not sent_via_harness:
        # Bark 手机推送降级
        if BARK_KEY:
            try:
                push_title_enc = urllib.parse.quote(desktop_title)
                push_body_enc = urllib.parse.quote(f"{desktop_body}\n\n{analysis.get('summary_text', '')}")
                bark_url = f"{BARK_SERVER}/{BARK_KEY}/{push_title_enc}/{push_body_enc}?group=mail-digest&icon=https://cdn-icons-png.flaticon.com/512/732/732200.png"
                req = urllib.request.Request(bark_url, headers={"User-Agent": "mail-guard/1.0"})
                urllib.request.urlopen(req, timeout=6)
            except Exception as e:
                print(f"[daily-summary] Bark 降级推送失败: {e}", file=sys.stderr)

        # 飞书 CLI (lark-cli) 降级
        send_feishu_lark_notification(desktop_title, analysis)

    print(f"[daily-summary] 通知流程完成: {desktop_title}")

def send_desktop_notification(title: str, message: str):
    """跨平台桌面原生弹窗 (自适应 macOS / Linux / Windows)"""
    import platform, subprocess, shutil
    plat = platform.system().lower()
    try:
        clean_msg = message.replace("\n", " | ").replace('"', '\\"')
        clean_title = title.replace('"', '\\"')
        if plat == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{clean_msg}" with title "{clean_title}" sound name "Glass"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif plat == "linux":
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", "-a", "mail-guard", clean_title, clean_msg],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
        elif plat == "windows":
            ps_cmd = (
                f"[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
                f"$notify = New-Object System.Windows.Forms.NotifyIcon; "
                f"$notify.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$notify.Visible = $true; "
                f"$notify.ShowBalloonTip(5000, '{clean_title}', '{clean_msg}', [System.Windows.Forms.ToolTipIcon]::Info)"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def send_feishu_lark_notification(title, analysis):
    """通过本机 lark-cli 将富文本 Markdown 汇总推送到用户本人飞书"""
    import shutil, subprocess
    lark_bin = shutil.which("lark-cli") or os.path.expanduser("~/.local/bin/lark-cli")
    if not os.path.exists(lark_bin):
        return

    user_id = FEISHU_USER or os.environ.get("FEISHU_USER_ID", "")
    if not user_id:
        return
    actions = analysis.get("action_items", [])
    alerts = analysis.get("important_alerts", [])
    summary_text = analysis.get("summary_text", "")

    md_lines = [
        f"**{title}**",
        f"> {summary_text}",
        ""
    ]
    if actions:
        md_lines.append(f"**🚨 待处理事项 ({len(actions)})**")
        for idx, a in enumerate(actions, 1):
            md_lines.append(f"{idx}. **[{a['account'].upper()}]** {a['subject']}\n   • 行动建议: {a['action_summary']}")
        md_lines.append("")

    if alerts:
        md_lines.append(f"**⚠️ 重要通知与安全提醒 ({len(alerts)})**")
        for idx, al in enumerate(alerts, 1):
            md_lines.append(f"{idx}. **[{al['account'].upper()}]** {al['subject']}\n   • 提醒要点: {al['alert_summary']}")

    md_content = "\n".join(md_lines)

    try:
        proc = subprocess.run(
            [lark_bin, "im", "+messages-send", "--user-id", user_id, "--markdown", md_content],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10
        )
        if proc.returncode == 0:
            print(f"[daily-summary] 飞书 CLI 消息已成功发送至用户本人 ({user_id})")
        else:
            err = proc.stderr.decode(errors="ignore").strip()
            print(f"[daily-summary] 飞书 CLI 发送失败: {err}", file=sys.stderr)
    except Exception as e:
        print(f"[daily-summary] 飞书 CLI 调用异常: {e}", file=sys.stderr)

def save_markdown_report(analysis, all_scanned_count):
    os.makedirs(SUMMARIES_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = os.path.join(SUMMARIES_DIR, f"summary_{timestamp}.md")

    actions = analysis.get("action_items", [])
    alerts = analysis.get("important_alerts", [])

    lines = [
        f"# 🛡️ mail-guard 邮箱汇总简报 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
        "",
        f"> **扫描总览**：共扫描 4 个邮箱最近收到的 {all_scanned_count} 封邮件，提炼出 **{len(actions)}** 项待办，**{len(alerts)}** 项关键提醒。",
        "",
        f"**总览概括**：{analysis.get('summary_text', '暂无概括')}",
        "",
        "---",
        "",
        "## 🚨 待处理事项 (Action Required)",
        ""
    ]

    if actions:
        for idx, item in enumerate(actions, 1):
            lines.extend([
                f"### {idx}. [{item['account'].upper()}] {item['subject']}",
                f"- **发件人**：`{item['from']}`",
                f"- **紧急度**：**{item.get('urgency', '中')}**",
                f"- **行动建议**：{item['action_summary']}",
                ""
            ])
    else:
        lines.append("*(当前无需要跟进的待办邮件)*\n")

    lines.extend([
        "## ⚠️ 重要通知与安全提醒",
        ""
    ])

    if alerts:
        for idx, item in enumerate(alerts, 1):
            lines.extend([
                f"### {idx}. [{item['account'].upper()}] {item['subject']}",
                f"- **发件人**：`{item['from']}`",
                f"- **要点说明**：{item['alert_summary']}",
                ""
            ])
    else:
        lines.append("*(当前无关键安全告警或重大变更通知)*\n")

    lines.append(f"\n*本报告由 mail-guard 自动化守护系统生成于 {datetime.now().isoformat()}*")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path

def main():
    parser = argparse.ArgumentParser(description="mail-guard: 每日跨邮箱重要邮件与待办智能汇总")
    parser.add_argument("--hours", type=int, default=8, help="扫描过去 N 小时内的邮件 (默认: 8)")
    parser.add_argument("--all-time", action="store_true", help="不限制时间，扫描全部可用范围")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，仅输出结果，不保存状态，不发送手机推送")
    parser.add_argument("--always-notify", action="store_true", help="即使没有重要/待办邮件也发送静默平安通报")
    args = parser.parse_args()

    accounts = load_accounts()
    state = load_state()
    watermarks = state.get("watermarks", {})

    print(f"[daily-summary] 开始执行跨邮箱汇总，账号列表: {list(accounts.keys())}...")

    all_mails = []
    new_watermarks = dict(watermarks)

    for acc_name, acc_cfg in accounts.items():
        last_uid = None if args.all_time else watermarks.get(acc_name)
        mails, max_uid = fetch_recent_emails(acc_name, acc_cfg, hours=args.hours, last_uid=last_uid)
        all_mails.extend(mails)
        if max_uid is not None:
            new_watermarks[acc_name] = max_uid
        print(f"  • [{acc_name}] 扫描到 {len(mails)} 封新邮件")

    print(f"[daily-summary] 邮件拉取完毕，总计 {len(all_mails)} 封。正在进行智能分类与提炼...")
    analysis = analyze_with_llm(all_mails)

    report_path = save_markdown_report(analysis, len(all_mails))
    print(f"[daily-summary] 汇总报告已生成: {report_path}")

    # 判断是否需要发送通知
    has_important = len(analysis.get("action_items", [])) > 0 or len(analysis.get("important_alerts", [])) > 0
    if not args.dry_run:
        if has_important or args.always_notify:
            send_notifications(analysis, report_path)
        else:
            print("[daily-summary] 周期内无重要待办，跳过推送保持静音 (使用 --always-notify 强制推送)")

        # 保存更新后的水位线状态
        state["last_run"] = datetime.now().isoformat()
        state["watermarks"] = new_watermarks
        save_state(state)
    else:
        print("[daily-summary] 演练模式：跳过通知发送与状态落盘。")

    # 控制台格式化输出
    print("\n" + "=" * 80)
    print(f"📋 汇总概览: {analysis.get('summary_text', '')}")
    print(f"🚨 待办事项 ({len(analysis.get('action_items', []))} 项):")
    for a in analysis.get("action_items", []):
        print(f"  [{a['account']}] {a['subject']} -> {a['action_summary']} (紧急度: {a.get('urgency', '中')})")
    print(f"⚠️ 重要提醒 ({len(analysis.get('important_alerts', []))} 项):")
    for al in analysis.get("important_alerts", []):
        print(f"  [{al['account']}] {al['subject']} -> {al['alert_summary']}")
    print("=" * 80)

if __name__ == "__main__":
    main()
