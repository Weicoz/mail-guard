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

def handle_daemon(args):
    action = args.action
    uid = os.getuid()

    if action == "status":
        print("=== 🛡️ mail-guard 后台服务状态 ===")
        # launchd 状态
        print("\n[LaunchAgents 状态]")
        subprocess.run(["launchctl", "list"], stdout=subprocess.PIPE, text=True)
        res = subprocess.run(["launchctl", "list"], stdout=subprocess.PIPE, text=True)
        for line in res.stdout.splitlines():
            if "com.weicoz.mailguard" in line:
                print(" ", line)

        # 实际进程状态
        print("\n[运行中进程]")
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

    elif action == "restart":
        print("正在重启 mail-guard 所有后台守护进程...")
        subprocess.run(["launchctl", "unload", IDLE_PLIST], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", "-w", IDLE_PLIST])
        subprocess.run(["launchctl", "unload", SUMMARY_PLIST], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", "-w", SUMMARY_PLIST])
        print("✅ 后台服务已成功重启并重载配置。")

    elif action == "start":
        print("正在加载并启动后台守护进程...")
        subprocess.run(["launchctl", "load", "-w", IDLE_PLIST])
        subprocess.run(["launchctl", "load", "-w", SUMMARY_PLIST])
        print("✅ 服务已启动。")

    elif action == "stop":
        print("正在停止后台守护进程...")
        subprocess.run(["launchctl", "unload", IDLE_PLIST])
        subprocess.run(["launchctl", "unload", SUMMARY_PLIST])
        print("🛑 服务已停止。")

    elif action == "logs":
        log_path = SUMMARY_LOG if args.summary else LOG_FILE
        if not os.path.exists(log_path):
            print(f"日志文件尚未生成: {log_path}")
            return
        print(f"=== 显示日志 ({log_path})，按 Ctrl+C 退出 ===")
        try:
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

ENV_FILE = os.path.join(CONFIG_DIR, "mail-guard.env")

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

def handle_notify(args):
    print("正在测试全通道消息推送 (飞书 CLI / Bark / macOS 桌面弹窗)...")
    env = load_env()
    user_id = env.get("FEISHU_USER") or os.environ.get("FEISHU_USER_ID", "")
    lark_bin = shutil.which("lark-cli") or os.path.expanduser("~/.local/bin/lark-cli")

    # 1. macOS 桌面弹窗
    os.system('osascript -e \'display notification "这是一条来自 mail-guard CLI 的通道自检测试通知" with title "📧 mail-guard 通道测试" sound name "Glass"\' 2>/dev/null || true')
    print("  • [macOS 桌面] 弹窗已触发")

    # 2. Bark 手机推送
    bark_key = env.get("BARK_KEY") or os.environ.get("BARK_KEY", "")
    if bark_key:
        try:
            import urllib.request, urllib.parse
            msg_enc = urllib.parse.quote("测试消息已收到")
            bark_server = env.get("BARK_SERVER", "https://api.day.app").rstrip("/")
            bark_url = f"{bark_server}/{bark_key}/mail-guard/{msg_enc}?group=mail-guard"
            urllib.request.urlopen(bark_url, timeout=5)
            print("  • [Bark 手机] 推送请求已发出")
        except Exception as e:
            print(f"  • [Bark 手机] 失败: {e}")
    else:
        print("  • [Bark 手机] 未配置 BARK_KEY，跳过")

    # 3. 飞书 CLI
    if not user_id:
        print("  • [飞书 CLI] 未在 mail-guard.env 配置 FEISHU_USER，跳过")
    elif os.path.exists(lark_bin):
        try:
            res = subprocess.run(
                [lark_bin, "im", "+messages-send", "--user-id", user_id, "--text", "【mail-guard CLI】消息通道测试：飞书专门推送至本人正常运作。"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            if res.returncode == 0:
                print(f"  • [飞书 CLI] 消息已专门发送至用户 ({user_id})")
            else:
                print(f"  • [飞书 CLI] 发送失败: {res.stderr.strip()}")
        except Exception as e:
            print(f"  • [飞书 CLI] 异常: {e}")
    else:
        print(f"  • [飞书 CLI] 未检测到可执行文件 {lark_bin}")

def handle_config(args):
    print("=== ⚙️ mail-guard 系统配置路径 ===")
    print(f"  • 项目源码目录:  {PROJECT_ROOT}")
    print(f"  • 用户配置目录:  {CONFIG_DIR}")
    print(f"  • 账号配置文件:  {ACCOUNTS_JSON}")
    print(f"  • 大模型配置:    {os.path.join(CONFIG_DIR, 'mail-guard.env')}")
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
    p_query.add_argument("--full", action="store_true", help="输出完整邮件正文")
    p_query.add_argument("--mark-read", action="store_true", help="将查询到的邮件标记为已读")

    # mail-guard summary
    p_summary = subparsers.add_parser("summary", help="执行跨邮箱待办与重要提醒智能汇总")
    p_summary.add_argument("--hours", type=int, default=8, help="扫描过去 N 小时邮件")
    p_summary.add_argument("--all-time", action="store_true", help="不限时间扫描")
    p_summary.add_argument("--dry-run", action="store_true", help="演练模式，仅打印不落盘发通知")
    p_summary.add_argument("--always-notify", action="store_true", help="即使无重要邮件也发送平安通报")

    # mail-guard daemon
    p_daemon = subparsers.add_parser("daemon", help="管理后台守护进程与定时服务")
    p_daemon.add_argument("action", choices=["status", "restart", "start", "stop", "logs"], help="守护操作")
    p_daemon.add_argument("--summary", action="store_true", help="查看定时汇总日志 (配合 logs 使用)")

    # mail-guard account
    p_account = subparsers.add_parser("account", help="查看与自检邮箱账户")
    p_account.add_argument("action", choices=["list", "test", "auth-ms"], help="账户操作")

    # mail-guard notify
    p_notify = subparsers.add_parser("notify", help="测试全通道消息推送连通性")

    # mail-guard config
    p_config = subparsers.add_parser("config", help="查看系统配置与存储路径")

    # 兼容直接传入 query 参数
    if len(sys.argv) > 1 and sys.argv[1] not in ["query", "summary", "daemon", "account", "notify", "config", "-h", "--help", "-v", "--version"]:
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
    elif args.subcommand == "config":
        handle_config(args)

if __name__ == "__main__":
    main()
