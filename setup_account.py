#!/usr/bin/env python3
"""
一键配置邮箱账户脚本
功能: 自动识别邮箱服务商、自动填写 IMAP 服务器与端口、更新配置并测试连接。
"""

import sys
import os
import subprocess

CONFIG_HIMALAYA = os.path.expanduser("~/.config/himalaya/config.toml")
CONFIG_NOTIFY = os.path.expanduser("~/.config/goimapnotify/goimapnotify.yaml")
POST_SHELL = os.path.expanduser("~/.config/mail-guard/handle_mail.py")

PRESETS = {
    "qq.com": {"host": "imap.qq.com", "port": 993, "name": "QQ邮箱"},
    "foxmail.com": {"host": "imap.qq.com", "port": 993, "name": "Foxmail"},
    "163.com": {"host": "imap.163.com", "port": 993, "name": "163网易邮箱"},
    "126.com": {"host": "imap.126.com", "port": 993, "name": "126网易邮箱"},
    "gmail.com": {"host": "imap.gmail.com", "port": 993, "name": "Gmail"},
    "outlook.com": {"host": "outlook.office365.com", "port": 993, "name": "Outlook"},
    "hotmail.com": {"host": "outlook.office365.com", "port": 993, "name": "Hotmail"},
    "exmail.qq.com": {"host": "imap.exmail.qq.com", "port": 993, "name": "腾讯企业邮"},
    "aliyun.com": {"host": "imap.aliyun.com", "port": 993, "name": "阿里云邮箱"},
}

def add_account(account_alias, email, password, host=None, port=993):
    domain = email.split("@")[-1].lower() if "@" in email else ""
    preset = PRESETS.get(domain)
    
    if not host:
        if preset:
            host = preset["host"]
            port = preset["port"]
            print(f"[识别成功] 自动匹配服务商: {preset['name']} ({host}:{port})")
        else:
            host = f"imap.{domain}"
            print(f"[未知域名] 尝试使用默认: {host}:{port}")

    # 1. 写入 himalaya 配置
    himalaya_block = f"""
[accounts.{account_alias}]
default = true
email = "{email}"
display-name = "{account_alias}"

backend.type = "imap"
backend.host = "{host}"
backend.port = {port}
backend.encryption = "tls"
backend.login = "{email}"
backend.auth.type = "password"
backend.auth.raw = "{password}"
"""
    os.makedirs(os.path.dirname(CONFIG_HIMALAYA), exist_ok=True)
    with open(CONFIG_HIMALAYA, "w", encoding="utf-8") as f:
        f.write(himalaya_block.strip() + "\n")

    # 2. 写入 goimapnotify 配置
    notify_block = f"""host: "{host}"
port: {port}
tls: true
tlsOptions:
  rejectUnauthorized: true
username: "{email}"
password: "{password}"

boxes:
  - "INBOX"
  - "Junk"
  - "Spam"

onNewMail: "{POST_SHELL} {account_alias} '%[mailbox]' '%[uid]'"
onNewMailPost: ""
wait: 1
"""
    os.makedirs(os.path.dirname(CONFIG_NOTIFY), exist_ok=True)
    with open(CONFIG_NOTIFY, "w", encoding="utf-8") as f:
        f.write(notify_block)

    print("\n[配置已保存] 正在测试邮箱连接...")
    # 3. 运行测试命令
    res = subprocess.run(["himalaya", "-a", account_alias, "envelope", "list", "-s", "1"], capture_output=True, text=True)
    if res.returncode == 0:
        print("✅ 登录验证成功！已成功连接到邮箱并读取列表！")
        return True
    else:
        print(f"❌ 登录失败，错误信息:\n{res.stderr.strip() or res.stdout.strip()}")
        return False

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        email = sys.argv[1]
        password = sys.argv[2]
        alias = sys.argv[3] if len(sys.argv) > 3 else "default"
        host = sys.argv[4] if len(sys.argv) > 4 else None
        port = int(sys.argv[5]) if len(sys.argv) > 5 else 993
        success = add_account(alias, email, password, host, port)
        sys.exit(0 if success else 1)
    else:
        print("=== 邮件账户配置助手 ===")
        email = input("请输入邮箱地址 (例如 your_name@qq.com): ").strip()
        pwd = input("请输入授权码/应用专用密码: ").strip()
        alias = input("请输入该邮箱别名 (回车默认为 default): ").strip() or "default"
        add_account(alias, email, pwd)
