#!/usr/bin/env python3
"""
mail-guard: 多账号 IMAP IDLE 守护进程管理器
读取 ~/.config/mail-guard/accounts.json，为每个账号生成专属 goimapnotify 配置并启动多进程监听。
海外邮箱按需注入 SOCKS5 代理环境。
"""

import os
import sys
import json
import time
import signal
import subprocess

CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
ACCOUNTS_CONFIG = os.path.join(CONFIG_DIR, "accounts.json")
NOTIFY_DIR = os.path.expanduser("~/.config/goimapnotify")
HANDLE_MAIL_SCRIPT = os.path.expanduser("~/.config/mail-guard/handle_mail.py")
if not os.path.exists(HANDLE_MAIL_SCRIPT):
    HANDLE_MAIL_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "handle_mail.py"))

processes = {}
running = True

def signal_handler(signum, frame):
    global running
    print(f"[mail-guard-daemon] 收到终止信号 ({signum})，正在停止所有监听进程...")
    running = False
    for name, proc in list(processes.items()):
        if proc.poll() is None:
            proc.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def probe_valid_mailboxes(acc_name: str, cfg: dict, candidate_boxes: list) -> list:
    """连接 IMAP 获取服务端真实存在的邮箱目录，过滤掉不存在的目录以彻底避免 goimapnotify 死循环退出"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import handle_mail
        mail = handle_mail.get_imap_connection(cfg)
        status, folder_list = mail.list()
        mail.logout()
        if status == 'OK' and folder_list:
            server_folders = set()
            for f in folder_list:
                if isinstance(f, bytes):
                    f = f.decode('utf-8', errors='ignore')
                name = f.split('\" \"')[-1].strip('\"') if '\" \"' in f else f.split()[-1].strip('\"')
                server_folders.add(name.lower())
            
            valid = [b for b in candidate_boxes if b.lower() in server_folders]
            if valid:
                print(f"[mail-guard-daemon] [{acc_name}] 动态探测可用目录: {valid}", flush=True)
                return valid
    except Exception as e:
        print(f"[mail-guard-daemon] [{acc_name}] 目录动态探测跳过 ({e})，使用默认配置", flush=True)

    return ["INBOX"]

def generate_notify_config(acc_name: str, cfg: dict) -> str:
    os.makedirs(NOTIFY_DIR, exist_ok=True)
    conf_path = os.path.join(NOTIFY_DIR, f"{acc_name}.yaml")
    
    # 针对不同服务商设置候选监视文件夹
    candidates = ["INBOX"]
    if "qq.com" in cfg.get("host", ""):
        candidates = ["INBOX", "Junk"]
    elif "gmail.com" in cfg.get("host", ""):
        candidates = ["INBOX"]

    boxes = probe_valid_mailboxes(acc_name, cfg, candidates)
    boxes_yaml = "\n".join([f'  - "{b}"' for b in boxes])
    
    if cfg.get("auth_type") == "xoauth2":
        ms_oauth_script = os.path.join(os.path.dirname(__file__), "ms_oauth2.py")
        auth_block = f"""passwordCmd: "{sys.executable} {ms_oauth_script} token"
xOAuth2: true"""
    else:
        auth_block = f'password: "{cfg["pass"]}"'

    on_new_mail_cmd = f"{sys.executable} {HANDLE_MAIL_SCRIPT} {acc_name} '%[mailbox]' '%[uid]'"
    content = f"""host: "{cfg['host']}"
port: {cfg.get('port', 993)}
tls: true
tlsOptions:
  rejectUnauthorized: true
username: "{cfg['user']}"
{auth_block}
boxes:
{boxes_yaml}
onNewMail: "{on_new_mail_cmd}"
"""
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(content)
    return conf_path

import shutil

GOIMAPNOTIFY_BIN = shutil.which("goimapnotify") or "goimapnotify"

def start_account_worker(acc_name: str, cfg: dict):
    conf_path = generate_notify_config(acc_name, cfg)
    env = os.environ.copy()
    if sys.platform != "win32":
        env["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
    proxy = cfg.get("proxy")
    if proxy:
        env["ALL_PROXY"] = f"socks5://{proxy}"
        print(f"[mail-guard-daemon] 启动 [{acc_name}] 监听进程 (经 SOCKS5 代理: {proxy})...", flush=True)
    else:
        env.pop("ALL_PROXY", None)
        print(f"[mail-guard-daemon] 启动 [{acc_name}] 监听进程 (直连)...", flush=True)

    proc = subprocess.Popen(
        [GOIMAPNOTIFY_BIN, "-conf", conf_path],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )
    return proc

def main():
    if not os.path.exists(ACCOUNTS_CONFIG):
        print(f"[mail-guard-daemon] 错误: 未找到配置文件 {ACCOUNTS_CONFIG}", file=sys.stderr)
        sys.exit(1)

    with open(ACCOUNTS_CONFIG, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    if not accounts:
        print("[mail-guard-daemon] accounts.json 中无有效账号", file=sys.stderr)
        sys.exit(1)

    print(f"[mail-guard-daemon] 守护进程启动，共加载 {len(accounts)} 个邮箱账号: {list(accounts.keys())}", flush=True)

    for name, cfg in accounts.items():
        processes[name] = start_account_worker(name, cfg)

    while running:
        time.sleep(15)
        for name, proc in list(processes.items()):
            poll = proc.poll()
            if poll is not None:
                err = proc.stderr.read().decode("utf-8", errors="ignore")[-200:]
                print(f"[mail-guard-daemon] 警告: 账号 [{name}] 监听进程意外退出 (代码 {poll}): {err.strip()}", flush=True)
                print(f"[mail-guard-daemon] 正在重新拉起账号 [{name}] 监听...", flush=True)
                processes[name] = start_account_worker(name, accounts[name])

if __name__ == "__main__":
    main()
