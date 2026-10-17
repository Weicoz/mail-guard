#!/usr/bin/env python3
"""
mail-guard: 事件驱动纯文本邮件过滤器与大模型提取器
核心安全保证:
1. 绝对不发起针对邮件内任何外部资源的 HTTP/DNS 请求，物理级阻断追踪与 IP 泄露。
2. 本地规则 0-Token 粗筛，非目标邮件直接丢弃，不消耗大模型配额。
3. 命中目标单次按需唤醒 LLM 提取结构化数据。
4. 调用用户自定义后壳脚本 (post_shell.sh)。
"""

import sys
import os
import re
import json
import socket
import struct
import subprocess
import urllib.request
import urllib.error
import imaplib
import ssl
import email
from email.header import decode_header

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
ACCOUNTS_CONFIG = os.path.join(CONFIG_DIR, "accounts.json")
NOTIFY_CONFIG = os.path.expanduser("~/.config/goimapnotify/goimapnotify.yaml")

def find_env_file():
    candidates = [
        os.path.join(PROJECT_ROOT, "mail-guard.env"),
        os.path.join(CONFIG_DIR, "mail-guard.env"),
        os.path.join(PROJECT_ROOT, "config", "mail-guard.env")
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def find_post_shell():
    candidates = [
        os.path.join(PROJECT_ROOT, "scripts", "post_shell.sh"),
        os.path.join(CONFIG_DIR, "post_shell.sh")
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def load_env():
    env = {}
    env_file = find_env_file()
    if env_file:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()
LLM_API_KEY = ENV.get("LLM_API_KEY", os.environ.get("LLM_API_KEY", ""))
LLM_BASE_URL = ENV.get("LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
LLM_MODEL = ENV.get("LLM_MODEL", os.environ.get("LLM_MODEL", "gpt-4o-mini"))

# 引入模型与 Harness 适配调度器
try:
    from llm_adapter import call_llm_or_harness
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from llm_adapter import call_llm_or_harness

KEYWORDS = [
    "验证码", "校验码", "动态码", "安全码", "授权码", "确认码",
    "verification code", "verify code", "security code", "auth code",
    "one-time password", "otp", "passcode", "reset password", "重置密码",
    "两步验证", "2fa", "two-factor",
    "confirm your email", "verify your email", "activate your account",
    "email confirmation", "确认你的邮箱", "激活账号"
]

LINK_REGEX = re.compile(r'(https?://[^\s<>"\'\)]+/(?:email_confirmation|confirm|verify|activate|auth)[^\s<>"\'\)]*)', re.IGNORECASE)

def pre_filter(subject: str, body: str) -> bool:
    content = f"{subject}\n{body}".lower()
    return any(kw.lower() in content for kw in KEYWORDS)

def regex_extract(subject: str, body: str):
    text = f"{subject}\n{body}"
    code_match = re.search(r'(?:验证码|动态码|code|Code|OTP|passcode)[^\d\w]{0,10}([0-9a-zA-Z]{4,8})\b', text)
    if not code_match:
        code_match = re.search(r'\b([0-9]{4,6})\b', text)
    
    link_match = LINK_REGEX.search(text)
    link = link_match.group(1) if link_match else None

    if code_match:
        code = code_match.group(1)
    elif link:
        code = f"链接确认: {link}"
    else:
        code = "未识别"
    
    exp_match = re.search(r'([0-9一二两三四五六七八九十]+(?:分钟|小时|秒|min|minute|hour)内?(?:有效)?)', text)
    expires = exp_match.group(1) if exp_match else "尽快使用"
    
    service_match = re.search(r'【([^】]+)】|\[([^\]]+)\]', subject)
    if service_match:
        service = service_match.group(1) or service_match.group(2)
    else:
        service = subject.split()[0] if subject else "未知服务"
        
    return {"code": code, "service": service, "expires_in": expires, "link": link}

def llm_extract(subject: str, body: str):
    prompt = f"""你是一个验证码与关键通知提取助手。请从下面这封邮件中提取核心信息。
邮件主题: {subject}
邮件正文:
{body[:2000]}

必须仅输出一段严格的合法 JSON，不要附带 Markdown 代码块，格式如下:
{{"code": "提取出的验证码/口令", "service": "发信机构或平台名称", "expires_in": "有效时限(若无填'尽快使用')"}}
"""
    try:
        data = call_llm_or_harness(prompt, expect_json=True, env=ENV, timeout=15)
        if data and isinstance(data, dict) and data.get("code"):
            link_match = LINK_REGEX.search(f"{subject}\n{body}")
            link = link_match.group(1) if link_match else None
            return {
                "code": str(data.get("code", "未识别")),
                "service": str(data.get("service", subject.split()[0] if subject else "未知服务")),
                "expires_in": str(data.get("expires_in", "尽快使用")),
                "link": link
            }
    except Exception as e:
        print(f"[mail-guard] 模型/Harness 调用异常 ({e})，回退到正则提取", file=sys.stderr)

    return regex_extract(subject, body)

def decode_mime_header(val):
    if not val: return ""
    parts = decode_header(val)
    res = []
    for text, enc in parts:
        if isinstance(text, bytes):
            res.append(text.decode(enc or 'utf-8', errors='ignore'))
        else:
            res.append(str(text))
    return "".join(res)

def extract_plain_text_from_msg(msg):
    """仅提取纯文本正文，绝对不请求任何远程内容"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get('Content-Disposition'))
            if ctype == 'text/plain' and 'attachment' not in cdisp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                body += payload.decode(charset, errors='ignore')
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        body = payload.decode(charset, errors='ignore') if payload else ""
    return body

def get_account_config(account_name: str):
    """根据账号名获取配置，优先读取 accounts.json，回退到 goimapnotify.yaml"""
    if os.path.exists(ACCOUNTS_CONFIG):
        try:
            with open(ACCOUNTS_CONFIG, "r", encoding="utf-8") as f:
                accounts = json.load(f)
                if account_name and account_name in accounts:
                    return accounts[account_name]
                if not account_name and accounts:
                    return next(iter(accounts.values()))
                # 尝试通过 user 字段匹配
                for cfg in accounts.values():
                    if cfg.get("user") == account_name:
                        return cfg
        except Exception as e:
            print(f"[mail-guard] 读取 accounts.json 失败: {e}", file=sys.stderr)

    # 回退到 goimapnotify.yaml
    cfg = {"host": "imap.qq.com", "port": 993, "user": "", "pass": "", "proxy": None}
    if os.path.exists(NOTIFY_CONFIG):
        with open(NOTIFY_CONFIG, "r", encoding="utf-8") as f:
            for line in f:
                line_s = line.strip()
                if line_s.startswith("host:"): cfg["host"] = line_s.split(":", 1)[1].strip().strip('"')
                elif line_s.startswith("port:"): cfg["port"] = int(line_s.split(":", 1)[1].strip())
                elif line_s.startswith("username:"): cfg["user"] = line_s.split(":", 1)[1].strip().strip('"')
                elif line_s.startswith("password:"): cfg["pass"] = line_s.split(":", 1)[1].strip().strip('"')
    return cfg

def get_imap_connection(acc_cfg: dict):
    """建立 IMAP 连接，原生支持 SOCKS5 代理分流与直连"""
    host = acc_cfg.get("host", "imap.qq.com")
    port = int(acc_cfg.get("port", 993))
    user = acc_cfg.get("user", "")
    pwd = acc_cfg.get("pass", "")
    proxy = acc_cfg.get("proxy")

    auth_type = acc_cfg.get("auth_type", "password")
    if auth_type != "xoauth2" and (not user or not pwd):
        raise ValueError("账号或密码为空")

    ctx = ssl.create_default_context()

    if proxy:
        proxy_host, proxy_port = proxy.split(":")
        proxy_port = int(proxy_port)
        s = socket.create_connection((proxy_host, proxy_port), timeout=10)
        s.sendall(b"\x05\x01\x00")
        auth = s.recv(2)
        if auth != b"\x05\x00":
            raise RuntimeError(f"SOCKS5 代理认证失败: {auth}")

        host_bytes = host.encode("utf-8")
        req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port)
        s.sendall(req)
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            err_code = resp[1] if len(resp) >= 2 else "未知"
            raise RuntimeError(f"SOCKS5 代理连接目标失败，响应码: {err_code}")

        sslsock = ctx.wrap_socket(s, server_hostname=host)

        class ProxiedIMAP(imaplib.IMAP4_SSL):
            def _create_socket(self, timeout):
                return sslsock

        mail = ProxiedIMAP(host, port, ssl_context=ctx)
    else:
        mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)

    if auth_type == "xoauth2":
        import ms_oauth2
        token = ms_oauth2.get_valid_access_token()
        if not token:
            raise RuntimeError(f"获取 {user} 的 OAuth2 Access Token 失败")
        auth_string = f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
        mail.authenticate("XOAUTH2", lambda x: auth_string)
    else:
        mail.login(user, pwd)
    return mail

def read_mail_by_uid(account: str, folder: str, uid: str):
    """通过本地 IMAP 仅抓取指定邮件的纯文本，物理阻断远程追踪"""
    acc_cfg = get_account_config(account)
    if not acc_cfg or not acc_cfg.get("user"):
        print(f"[mail-guard] 未找到账号 '{account}' 的配置", file=sys.stderr)
        return "", ""

    try:
        mail = get_imap_connection(acc_cfg)
        mail.select(folder or "INBOX")
        res, msg_data = mail.uid('fetch', str(uid), '(RFC822)')
        mail.logout()
        if res == 'OK' and msg_data and msg_data[0]:
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject"))
            body = extract_plain_text_from_msg(msg)
            return subject, body
    except Exception as e:
        print(f"[mail-guard] IMAP 读取失败 ({account}): {e}", file=sys.stderr)
    return "", ""

def main():
    account = sys.argv[1] if len(sys.argv) > 1 else ""
    folder = sys.argv[2] if len(sys.argv) > 2 else ""
    uid = sys.argv[3] if len(sys.argv) > 3 else ""

    if not uid:
        print("用法: handle_mail.py <account> <folder> <uid>")
        sys.exit(1)

    print(f"[mail-guard] 收到新邮件通知: 账号={account}, 文件夹={folder}, UID={uid}")
    subject, body = read_mail_by_uid(account, folder, uid)
    if not subject and not body:
        print("[mail-guard] 获取邮件内容为空或失败，退出")
        sys.exit(0)

    # 1. 本地零 Token 预筛选
    if not pre_filter(subject, body):
        print(f"[mail-guard] 非目标/非验证码邮件 ('{subject}')，跳过 (消耗 0 Token)")
        sys.exit(0)

    print(f"[mail-guard] 命中验证码关键词，唤醒大模型进行提取...")
    result = llm_extract(subject, body)
    
    code = result.get("code", "未识别")
    service = result.get("service", "未知")
    expires_in = result.get("expires_in", "")

    print(f"[mail-guard] 提取成功: [{service}] 验证码: {code} ({expires_in})")

    # 2. 触发用户后壳脚本
    post_shell = find_post_shell()
    if post_shell and os.access(post_shell, os.X_OK):
        print(f"[mail-guard] 执行后壳: {post_shell}")
        subprocess.run([post_shell, code, service, expires_in, subject, account])

if __name__ == "__main__":
    main()
