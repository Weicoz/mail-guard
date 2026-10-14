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
import subprocess
import urllib.request
import urllib.error
import imaplib
import ssl
import email
from email.header import decode_header

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
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

KEYWORDS = [
    "验证码", "校验码", "动态码", "安全码", "授权码", "确认码",
    "verification code", "verify code", "security code", "auth code",
    "one-time password", "otp", "passcode", "reset password", "重置密码",
    "两步验证", "2fa", "two-factor"
]

def pre_filter(subject: str, body: str) -> bool:
    content = f"{subject}\n{body}".lower()
    return any(kw.lower() in content for kw in KEYWORDS)

def regex_extract(subject: str, body: str):
    text = f"{subject}\n{body}"
    code_match = re.search(r'(?:验证码|动态码|code|Code|OTP|passcode)[^\d\w]{0,10}([0-9a-zA-Z]{4,8})\b', text)
    if not code_match:
        code_match = re.search(r'\b([0-9]{4,6})\b', text)
    code = code_match.group(1) if code_match else "未识别"
    
    exp_match = re.search(r'([0-9一二两三四五六七八九十]+(?:分钟|小时|秒|min|minute|hour)内?(?:有效)?)', text)
    expires = exp_match.group(1) if exp_match else "尽快使用"
    
    service_match = re.search(r'【([^】]+)】|\[([^\]]+)\]', subject)
    if service_match:
        service = service_match.group(1) or service_match.group(2)
    else:
        service = subject.split()[0] if subject else "未知服务"
        
    return {"code": code, "service": service, "expires_in": expires}

def llm_extract(subject: str, body: str):
    if not LLM_API_KEY:
        return regex_extract(subject, body)
    prompt = f"""你是一个验证码与关键通知提取助手。请从下面这封邮件中提取核心信息。
邮件主题: {subject}
邮件正文:
{body[:2000]}

必须仅输出一段严格的合法 JSON，不要附带 Markdown 代码块，格式如下:
{{"code": "提取出的验证码/口令", "service": "发信机构或平台名称", "expires_in": "有效时限(若无填'尽快使用')"}}
"""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"}
    payload = {"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
    req = urllib.request.Request(f"{LLM_BASE_URL}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r'^```json\s*', '', content)
            content = re.sub(r'```$', '', content).strip()
            return json.loads(content)
    except Exception as e:
        print(f"[mail-guard] LLM 调用失败或超时 ({e})，回退到正则提取", file=sys.stderr)
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

def read_mail_by_uid(folder: str, uid: str):
    """通过本地 IMAP 仅抓取指定邮件的文本，无外链加载风险"""
    # 从配置文件读取账号与密码
    host, port, user, pwd = "imap.qq.com", 993, "", ""
    if os.path.exists(NOTIFY_CONFIG):
        with open(NOTIFY_CONFIG, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("host:"): host = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("port:"): port = int(line.split(":", 1)[1].strip())
                elif line.startswith("username:"): user = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("password:"): pwd = line.split(":", 1)[1].strip().strip('"')
    if not user or not pwd:
        return "", ""
    try:
        ctx = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        mail.login(user, pwd)
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
        print(f"[mail-guard] IMAP 读取失败: {e}", file=sys.stderr)
    return "", ""

def main():
    account = sys.argv[1] if len(sys.argv) > 1 else ""
    folder = sys.argv[2] if len(sys.argv) > 2 else ""
    uid = sys.argv[3] if len(sys.argv) > 3 else ""

    if not uid:
        print("用法: handle_mail.py <account> <folder> <uid>")
        sys.exit(1)

    print(f"[mail-guard] 收到新邮件通知: 账号={account}, 文件夹={folder}, UID={uid}")
    subject, body = read_mail_by_uid(folder, uid)
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
