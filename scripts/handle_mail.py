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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")

# 寻找环境变量文件 (优先项目根目录，其次 ~/.config)
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

# 寻找用户后壳脚本
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

# 本地粗筛关键词 (0-Token 过滤，命中才唤醒大模型)
KEYWORDS = [
    "验证码", "校验码", "动态码", "安全码", "授权码", "确认码",
    "verification code", "verify code", "security code", "auth code",
    "one-time password", "otp", "passcode", "reset password", "重置密码",
    "两步验证", "2fa", "two-factor"
]

def pre_filter(subject: str, body: str) -> bool:
    """本地粗筛，匹配失败直接退出，不消耗任何 Token"""
    content = f"{subject}\n{body}".lower()
    return any(kw.lower() in content for kw in KEYWORDS)

def regex_extract(subject: str, body: str):
    """本地高精度正则兜底提取 (免 Token)"""
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
    """单次调用大模型进行精准结构化提取"""
    if not LLM_API_KEY:
        return regex_extract(subject, body)
    
    prompt = f"""你是一个验证码与关键通知提取助手。请从下面这封邮件中提取核心信息。
邮件主题: {subject}
邮件正文:
{body[:2000]}

必须仅输出一段严格的合法 JSON，不要附带 Markdown 代码块，格式如下:
{{"code": "提取出的验证码/口令", "service": "发信机构或平台名称", "expires_in": "有效时限(若无填'尽快使用')"}}
"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r'^```json\s*', '', content)
            content = re.sub(r'```$', '', content).strip()
            return json.loads(content)
    except Exception as e:
        print(f"[mail-guard] LLM 调用失败或超时 ({e})，自动回退到本地正则提取", file=sys.stderr)
        return regex_extract(subject, body)

def read_via_himalaya(account: str, folder: str, msg_id: str):
    """调用 himalaya 仅读取纯文本，绝对不加载任何远程网络资源"""
    cmd = ["himalaya", "-o", "json"]
    if account:
        cmd.extend(["-a", account])
    if folder:
        cmd.extend(["-f", folder])
    cmd.extend(["message", "read", str(msg_id)])
    
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        subject = data.get("subject", "")
        body = data.get("body", "") or data.get("text", "")
        return subject, body
    except Exception as e:
        print(f"[mail-guard] Himalaya 读取失败: {e}", file=sys.stderr)
        return "", ""

def main():
    account = sys.argv[1] if len(sys.argv) > 1 else ""
    folder = sys.argv[2] if len(sys.argv) > 2 else ""
    msg_id = sys.argv[3] if len(sys.argv) > 3 else ""

    if not msg_id:
        print("用法: handle_mail.py <account> <folder> <msg_id>")
        sys.exit(1)

    print(f"[mail-guard] 收到新邮件通知: 账号={account}, 文件夹={folder}, ID={msg_id}")
    
    subject, body = read_via_himalaya(account, folder, msg_id)
    if not subject and not body:
        print("[mail-guard] 获取邮件内容为空或失败，退出")
        sys.exit(0)

    # 1. 本地零 Token 预筛选
    if not pre_filter(subject, body):
        print(f"[mail-guard] 非目标邮件 ('{subject}')，跳过 (消耗 0 Token)")
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
    else:
        print(f"[mail-guard] 后壳脚本未找到或无执行权限: {post_shell}")

if __name__ == "__main__":
    main()
