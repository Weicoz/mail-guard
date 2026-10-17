#!/usr/bin/env python3
"""
mail-guard 统一命令行工具 (mail-guard CLI)

命令总览:
  mail-guard query      主动按需查询邮件、搜索关键词或提取最新验证码
  mail-guard summary    执行或演练跨邮箱每日智能汇总 (4次/天)
  mail-guard daemon     管理后台守护服务 (status, restart, start, stop, logs)
  mail-guard account    查看邮箱账号状态、测试各邮箱连通性或触发微软 OAuth2 重新授权
  mail-guard notify     测试通知通道连通性 (飞书 CLI / Bark / macOS 桌面弹窗)
  mail-guard config     查看当前系统所有配置文件与存储路径
"""

import sys
import os
import subprocess
import argparse
import json
import shutil

SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
ACCOUNTS_JSON = os.path.join(CONFIG_DIR, "accounts.json")
LOG_FILE = os.path.join(CONFIG_DIR, "mailguard.log")
SUMMARY_LOG = os.path.join(CONFIG_DIR, "summary.log")

IDLE_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.weicoz.mailguard.plist")
SUMMARY_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.weicoz.mailguard.summary.plist")
ENV_FILE = os.path.join(CONFIG_DIR, "mail-guard.env")

# 默认排班时刻表 (约 3 小时 6 班制，00:30~08:30 深夜免打扰)
DEFAULT_SCHEDULE = ["08:30", "12:00", "15:30", "18:30", "21:30", "00:30"]

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

def parse_schedule(schedule_str: str = None):
    """解析并校验时刻表字符串，格式如 '08:30, 12:00, 15:30'"""
    if not schedule_str:
        return DEFAULT_SCHEDULE.copy(), False
    items = [x.strip() for x in schedule_str.split(",") if x.strip()]
    valid = []
    for item in items:
        parts = item.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
    if valid:
        return valid, True
    return DEFAULT_SCHEDULE.copy(), False

def get_current_schedule():
    env = load_env()
    val = env.get("SUMMARY_SCHEDULE", "").strip()
    return parse_schedule(val)

def generate_launchd_summary_plist(schedule: list) -> str:
    """根据给定的时刻表动态生成 LaunchAgents plist XML 内容"""
    python_bin = sys.executable
    project_dir = PROJECT_ROOT
    home_dir = os.path.expanduser("~")
    local_bin = os.path.join(home_dir, ".local", "bin")

    intervals_xml = []
    for t in schedule:
        h, m = map(int, t.split(":"))
        intervals_xml.append(f"""        <dict>
            <key>Hour</key>
            <integer>{h}</integer>
            <key>Minute</key>
            <integer>{m}</integer>
        </dict>""")
    intervals_str = "\n".join(intervals_xml)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.weicoz.mailguard.summary</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{os.path.join(project_dir, 'scripts', 'daily_summary.py')}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{local_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
{intervals_str}
    </array>
    <key>StandardOutPath</key>
    <string>{os.path.join(CONFIG_DIR, 'summary.log')}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.join(CONFIG_DIR, 'summary.err')}</string>
</dict>
</plist>
"""

def generate_systemd_timer(schedule: list) -> str:
    """根据给定的时刻表动态生成 Linux systemd timer 内容"""
    lines = [
        "[Unit]",
        "Description=mail-guard scheduled summary timer",
        "After=network.target",
        "",
        "[Timer]"
    ]
    for t in schedule:
        lines.append(f"OnCalendar=*-*-* {t}:00")
    lines.extend([
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        ""
    ])
    return "\n".join(lines)

def sync_schedule_to_service(verbose=True):
    import platform
    plat = platform.system().lower()
    schedule, is_custom = get_current_schedule()

    if plat == "darwin":
        plist_content = generate_launchd_summary_plist(schedule)
        os.makedirs(os.path.dirname(SUMMARY_PLIST), exist_ok=True)
        with open(SUMMARY_PLIST, "w", encoding="utf-8") as f:
            f.write(plist_content)
        subprocess.run(["launchctl", "unload", SUMMARY_PLIST], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", "-w", SUMMARY_PLIST], stderr=subprocess.DEVNULL)
        if verbose:
            tag = "自定义" if is_custom else "默认"
            print(f"✔ macOS LaunchAgents 汇总排班已同步生效 [{tag}]: {', '.join(schedule)}")

    elif plat == "linux":
        timer_content = generate_systemd_timer(schedule)
        timer_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(timer_dir, exist_ok=True)
        timer_file = os.path.join(timer_dir, "mailguard-summary.timer")
        with open(timer_file, "w", encoding="utf-8") as f:
            f.write(timer_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl", "--user", "restart", "mailguard-summary.timer"], stderr=subprocess.DEVNULL)
        if verbose:
            tag = "自定义" if is_custom else "默认"
            print(f"✔ Linux systemd 汇总排班已同步生效 [{tag}]: {', '.join(schedule)}")
    elif plat == "windows":
        if verbose:
            tag = "自定义" if is_custom else "默认"
            print(f"ℹ Windows 环境排班已更新 [{tag}]: {', '.join(schedule)}")

def run_script(script_name, extra_args):
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        print(f"错误: 未找到脚本 {script_path}", file=sys.stderr)
        sys.exit(1)
    cmd = [sys.executable, script_path] + extra_args
    try:
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        sys.exit(130)

def send_desktop_notification(title: str, message: str):
    """跨平台桌面原生弹窗 (自适应 macOS / Linux / Windows)"""
    import platform
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

def handle_daemon(args):
    action = args.action
    import platform
    plat = platform.system().lower()

    if action == "status":
        print(f"=== 🛡️ mail-guard 后台服务状态 [{plat.upper()}] ===")
        if plat == "darwin":
            print("\n[LaunchAgents 状态]")
            res = subprocess.run(["launchctl", "list"], stdout=subprocess.PIPE, text=True)
            for line in res.stdout.splitlines():
                if "com.weicoz.mailguard" in line:
                    print(" ", line)
        elif plat == "linux":
            print("\n[systemd 用户服务状态]")
            subprocess.run(["systemctl", "--user", "status", "mailguard.service", "--no-pager"])
            subprocess.run(["systemctl", "--user", "status", "mailguard-summary.timer", "--no-pager"])
        elif plat == "windows":
            print("\n[Windows 任务进程]")
            subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"])

        # 实际进程状态
        print("\n[运行中进程]")
        if plat == "windows":
            subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, Path"])
        else:
            res_ps = subprocess.run(["ps", "aux"], stdout=subprocess.PIPE, text=True)
            running = []
            for line in res_ps.stdout.splitlines():
                if any(k in line for k in ["goimapnotify", "run_daemon.py", "daily_summary.py"]) and "grep" not in line and "cli.py" not in line:
                    running.append(line)
            if running:
                for r in running:
                    cols = r.split(None, 10)
                    pid = cols[1]
                    cmd = cols[10] if len(cols) > 10 else ""
                    print(f"  • PID {pid}: {cmd}")
            else:
                print("  (当前无常驻 worker 进程)")

    elif action == "schedule":
        schedule, is_custom = get_current_schedule()
        tag = "自定义 (来自 mail-guard.env)" if is_custom else "默认班次 (约 3 小时 6 班制)"
        print(f"=== ⏰ mail-guard 定时汇总排班 [{tag}] ===")
        print(f"  • 时刻列表:    {', '.join(schedule)}")
        print(f"  • 配置文件:    {ENV_FILE}")
        print("  • 说明: 可在 mail-guard.env 中设置 SUMMARY_SCHEDULE=\"08:30, 12:00, ...\" 调整时刻")
        if getattr(args, "apply", False):
            sync_schedule_to_service(verbose=True)
        else:
            print("  • 提示: 加上 --apply 参数立即同步并重载系统服务 (例如: mail-guard daemon schedule --apply)")

    elif action == "restart":
        print("正在重启 mail-guard 所有后台守护进程并同步最新排班...")
        sync_schedule_to_service(verbose=False)
        if plat == "darwin":
            subprocess.run(["launchctl", "unload", IDLE_PLIST], stderr=subprocess.DEVNULL)
            subprocess.run(["launchctl", "load", "-w", IDLE_PLIST])
            subprocess.run(["launchctl", "unload", SUMMARY_PLIST], stderr=subprocess.DEVNULL)
            subprocess.run(["launchctl", "load", "-w", SUMMARY_PLIST])
        elif plat == "linux":
            subprocess.run(["systemctl", "--user", "restart", "mailguard.service"])
            subprocess.run(["systemctl", "--user", "restart", "mailguard-summary.timer"])
        elif plat == "windows":
            script_path = os.path.join(SCRIPTS_DIR, "run_daemon.py")
            subprocess.Popen([sys.executable, script_path], creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        print("✅ 后台服务已成功重启，排班与配置已同步重载。")

    elif action == "start":
        print("正在加载并启动后台守护进程...")
        if plat == "darwin":
            subprocess.run(["launchctl", "load", "-w", IDLE_PLIST])
            subprocess.run(["launchctl", "load", "-w", SUMMARY_PLIST])
        elif plat == "linux":
            subprocess.run(["systemctl", "--user", "start", "mailguard.service"])
            subprocess.run(["systemctl", "--user", "start", "mailguard-summary.timer"])
        elif plat == "windows":
            script_path = os.path.join(SCRIPTS_DIR, "run_daemon.py")
            subprocess.Popen([sys.executable, script_path], creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        print("✅ 服务已启动。")

    elif action == "stop":
        print("正在停止后台守护进程...")
        if plat == "darwin":
            subprocess.run(["launchctl", "unload", IDLE_PLIST])
            subprocess.run(["launchctl", "unload", SUMMARY_PLIST])
        elif plat == "linux":
            subprocess.run(["systemctl", "--user", "stop", "mailguard.service"])
            subprocess.run(["systemctl", "--user", "stop", "mailguard-summary.timer"])
        elif plat == "windows":
            print("Windows 环境请在任务管理器中终止 python 守护进程。")
        print("🛑 服务已停止。")

    elif action == "logs":
        log_path = SUMMARY_LOG if args.summary else LOG_FILE
        if not os.path.exists(log_path):
            print(f"日志文件尚未生成: {log_path}")
            return
        print(f"=== 显示日志 ({log_path})，按 Ctrl+C 退出 ===")
        try:
            if plat == "windows":
                subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-Content '{log_path}' -Tail 30 -Wait"])
            else:
                subprocess.run(["tail", "-n", "30", "-f", log_path])
        except KeyboardInterrupt:
            pass

def handle_account(args):
    action = args.action

    if not os.path.exists(ACCOUNTS_JSON):
        print(f"错误: 配置文件不存在 ({ACCOUNTS_JSON})", file=sys.stderr)
        return

    with open(ACCOUNTS_JSON, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    if action == "list":
        print(f"已配置 {len(accounts)} 个邮箱账号:")
        for name, cfg in accounts.items():
            auth = cfg.get("auth_type", "password")
            proxy = cfg.get("proxy") or "直连"
            print(f"  • [{name}] {cfg.get('user')} (服务器: {cfg.get('host')}:{cfg.get('port', 993)}, 认证方式: {auth}, 代理: {proxy})")

    elif action == "test":
        print("正在自检所有邮箱连接与鉴权状态...")
        sys.path.insert(0, SCRIPTS_DIR)
        from query_mail import connect_imap
        for name, cfg in accounts.items():
            print(f"  测试 [{name}] ({cfg.get('user')})...", end=" ", flush=True)
            try:
                mail = connect_imap(name, cfg)
                mail.select("INBOX", readonly=True)
                res, data = mail.uid("search", None, "ALL")
                total = len(data[0].split()) if (res == "OK" and data[0]) else 0
                mail.logout()
                print(f"✅ 成功! (收件箱共 {total} 封)")
            except Exception as e:
                print(f"❌ 失败: {e}")

    elif action == "auth-ms":
        print("启动微软 Outlook 现代认证 (Device Code)...")
        ms_script = os.path.join(SCRIPTS_DIR, "ms_oauth2.py")
        subprocess.run([sys.executable, ms_script, "login"])

def handle_notify(args):
    print("正在测试全通道消息推送 (harness-notify 统一组件 / 飞书 CLI / Bark / 原生桌面弹窗)...")
    env = load_env()
    user_id = env.get("FEISHU_USER") or os.environ.get("FEISHU_USER_ID", "")
    lark_bin = shutil.which("lark-cli") or os.path.expanduser("~/.local/bin/lark-cli")
    harness_bin = shutil.which("harness-notify") or os.path.expanduser("~/.local/bin/harness-notify")

    # 1. 桌面原生弹窗 (自适应 macOS / Linux / Windows)
    send_desktop_notification("📧 mail-guard 通道测试", "这是一条来自 mail-guard CLI 的跨平台通道自检测试通知")
    print("  • [原生桌面] 弹窗已触发")

    # 2. harness-notify 统一多渠道调度器
    if os.path.exists(harness_bin) and os.access(harness_bin, os.X_OK):
        print("  • [harness-notify] 发现统一通知调度器，正在派发测试消息...")
        res = subprocess.run(
            [harness_bin, "send", "--harness", "mailguard", "--title", "CLI 通道测试", "--body", "这是一条来自 mail-guard CLI 的通道自检通知，验证 harness-notify 统一多渠道分发是否畅通。"],
            capture_output=True,
            text=True,
            timeout=15
        )
        if res.returncode == 0:
            print("  • [harness-notify] ✅ 统一推送测试成功！(已按 routes 分发至配置渠道)")
        else:
            err = res.stderr.strip() or res.stdout.strip()
            print(f"  • [harness-notify] ❌ 发送返回异常: {err}")
    else:
        print("  • [harness-notify] 未检测到 harness-notify 可执行文件")

    # 3. 独立 Bark 手机推送 (备用通道状态检查)
    bark_key = env.get("BARK_KEY") or os.environ.get("BARK_KEY", "")
    if bark_key:
        try:
            import urllib.request, urllib.parse
            msg_enc = urllib.parse.quote("测试消息已收到 (独立备用直连)")
            bark_server = env.get("BARK_SERVER", "https://api.day.app").rstrip("/")
            bark_url = f"{bark_server}/{bark_key}/mail-guard/{msg_enc}?group=mail-guard"
            urllib.request.urlopen(bark_url, timeout=5)
            print("  • [Bark 独立] 备用直连请求已发出")
        except Exception as e:
            print(f"  • [Bark 独立] 失败: {e}")
    else:
        print("  • [Bark 独立] 未在 mail-guard.env 配置 BARK_KEY")

    # 4. 独立飞书 CLI (备用通道状态检查)
    if not user_id:
        print("  • [飞书独立] 未在 mail-guard.env 配置 FEISHU_USER")
    elif os.path.exists(lark_bin):
        print(f"  • [飞书独立] 检测到 lark-cli，备用直连就绪 (用户: {user_id})")
    else:
        print(f"  • [飞书独立] 未检测到可执行文件 {lark_bin}")

def handle_model(args):
    try:
        from llm_adapter import get_harness_status_report, HARNESS_PROFILES, call_llm_or_harness
    except ImportError:
        sys.path.insert(0, SCRIPTS_DIR)
        from llm_adapter import get_harness_status_report, HARNESS_PROFILES, call_llm_or_harness

    report = get_harness_status_report()
    print("=== 🤖 mail-guard 模型与 Harness 调度状态 ===")
    print(f"  • 当前模式 (LLM_PROVIDER): {report['provider']}")
    print(f"  • 当前主用路由:             {report['active_route']}")
    print(f"  • API Key 状态:             {report['api_key_masked']}")
    if report.get("api_key_configured"):
        print(f"  • API 接口地址:             {report['base_url']}")
        print(f"  • 默认模型名称:             {report['model']}")
    print(f"\n[本机 Harness 探测清单]")
    for h_id, prof in HARNESS_PROFILES.items():
        p = report["available_harnesses"].get(h_id)
        tag = f"✅ 可用 ({p})" if p else "❌ 未安装"
        print(f"  • {prof['name']:<20} ({h_id:<8}): {tag}")

    if report["default_harness"]:
        print(f"\n  • 自动推荐优先 Harness: {report['default_harness']}")

    if getattr(args, "action", None) == "test":
        import time
        print("\n>> 正在执行端到端轻量提取测试...")
        t0 = time.time()
        test_prompt = """请从以下邮件中提取验证码与发件服务:
主题: 【GitHub】Your two-factor authentication code
正文: Verification code: 829104. Expire in 10 minutes.
输出合法 JSON: {"code": "验证码", "service": "发信机构", "expires_in": "有效时限"}
"""
        res = call_llm_or_harness(test_prompt, expect_json=True)
        cost = time.time() - t0
        print(f"  • 测试耗时: {cost:.2f}s")
        print(f"  • 提取结果: {res}")
        if res and res.get("code") == "829104":
            print("  • 验证状态: ✅ 模型/Harness 结构化提取成功！")
        else:
            print("  • 验证状态: ⚠️ 结构化提取未返回预期结果，系统将安全使用规则引擎容灾。")

def handle_config(args):
    print("=== ⚙️ mail-guard 系统配置路径 ===")
    print(f"  • 项目源码目录:  {PROJECT_ROOT}")
    print(f"  • 用户配置目录:  {CONFIG_DIR}")
    print(f"  • 账号配置文件:  {ACCOUNTS_JSON}")
    print(f"  • 核心环境变量:  {ENV_FILE}")
    try:
        from llm_adapter import get_harness_status_report
        m_rep = get_harness_status_report()
        print(f"  • 模型驱动方式:  {m_rep['active_route']}")
    except Exception:
        pass
    print(f"  • 统一通知配置:  {os.path.expanduser('~/.config/harness-notify/config.json')}")
    schedule, is_custom = get_current_schedule()
    tag = "自定义 (来自 mail-guard.env)" if is_custom else "默认班次 (约 3 小时 6 班制)"
    print(f"  • 每日汇总排班:  {', '.join(schedule)} [{tag}]")
    print(f"  • 微软凭据缓存:  {os.path.join(CONFIG_DIR, 'ms_tokens.json')}")
    print(f"  • 汇总状态缓存:  {os.path.join(CONFIG_DIR, 'summary_state.json')}")
    print(f"  • 历史简报归档:  {os.path.join(CONFIG_DIR, 'summaries')}")
    print(f"  • 守护日志文件:  {LOG_FILE}")
    print(f"  • 定时汇总日志:  {SUMMARY_LOG}")

def main():
    parser = argparse.ArgumentParser(
        prog="mail-guard",
        description="🛡️ mail-guard: 安全隐私优先的跨邮箱管理与大模型智能通知 CLI"
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="可用子命令")

    # mail-guard query
    p_query = subparsers.add_parser("query", help="主动查询邮箱内容/验证码/过滤发件人")
    p_query.add_argument("--account", "-a", default="all", help="邮箱账号 (qq, gmail1, gmail2, outlook, all)")
    p_query.add_argument("--limit", "-n", type=int, default=1, help="每个账号返回的最大邮件数")
    p_query.add_argument("--keyword", "-k", default="", help="关键词检索")
    p_query.add_argument("--from", "-f", dest="from_filter", default="", help="发件人检索")
    p_query.add_argument("--subject", "-s", default="", help="主题检索")
    p_query.add_argument("--days", "-d", type=int, default=30, help="过去 N 天内")
    p_query.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_query.add_argument("--folder", default="INBOX", help="指定 IMAP 文件夹 (如 INBOX, Junk)")
    p_query.add_argument("--full", action="store_true", help="输出完整邮件正文")
    p_query.add_argument("--mark-read", action="store_true", help="将查询到的邮件标记为已读")
    p_query.add_argument("--live", action="store_true", help="强制穿透缓存直连 IMAP 邮件服务器获取最新邮件 (默认优先读取秒级本地热快照)")

    # mail-guard summary
    p_summary = subparsers.add_parser("summary", help="执行跨邮箱待办与重要提醒智能汇总")
    p_summary.add_argument("--hours", type=int, default=8, help="扫描过去 N 小时邮件")
    p_summary.add_argument("--all-time", action="store_true", help="不限时间扫描")
    p_summary.add_argument("--dry-run", action="store_true", help="演练模式，仅打印不落盘发通知")
    p_summary.add_argument("--always-notify", action="store_true", help="即使无重要邮件也发送平安通报")

    # mail-guard daemon
    p_daemon = subparsers.add_parser("daemon", help="管理后台守护进程与定时服务")
    p_daemon.add_argument("action", choices=["status", "restart", "start", "stop", "logs", "schedule"], help="守护操作")
    p_daemon.add_argument("--summary", action="store_true", help="查看定时汇总日志 (配合 logs 使用)")
    p_daemon.add_argument("--apply", action="store_true", help="立即同步并应用配置文件中的排班到系统服务 (配合 schedule 使用)")

    # mail-guard model
    p_model = subparsers.add_parser("model", help="查看与测试大模型/Harness 调度配置与连通性")
    p_model.add_argument("action", nargs="?", default="status", choices=["status", "test"], help="操作: status (查看状态), test (执行测试提取)")

    # mail-guard account
    p_account = subparsers.add_parser("account", help="查看与自检邮箱账户")
    p_account.add_argument("action", choices=["list", "test", "auth-ms"], help="账户操作")

    # mail-guard notify
    p_notify = subparsers.add_parser("notify", help="测试全通道消息推送连通性")

    # mail-guard config
    p_config = subparsers.add_parser("config", help="查看系统配置与存储路径")

    # mail-guard test
    p_test = subparsers.add_parser("test", help="执行跨平台端到端功能自检流水线")

    # 兼容直接传入 query 参数
    if len(sys.argv) > 1 and sys.argv[1] not in ["query", "summary", "daemon", "account", "notify", "model", "config", "test", "-h", "--help", "-v", "--version"]:
        # 将参数透传给 query
        run_script("query_mail.py", sys.argv[1:])
        return

    args, extra = parser.parse_known_args()

    if not args.subcommand:
        parser.print_help()
        sys.exit(0)

    if args.subcommand == "query":
        run_script("query_mail.py", sys.argv[2:])
    elif args.subcommand == "summary":
        run_script("daily_summary.py", sys.argv[2:])
    elif args.subcommand == "daemon":
        handle_daemon(args)
    elif args.subcommand == "account":
        handle_account(args)
    elif args.subcommand == "notify":
        handle_notify(args)
    elif args.subcommand == "model":
        handle_model(args)
    elif args.subcommand == "test":
        run_script("test_pipeline.py", sys.argv[2:])
    elif args.subcommand == "config":
        handle_config(args)

if __name__ == "__main__":
    main()
