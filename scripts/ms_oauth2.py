#!/usr/bin/env python3
"""
mail-guard: 微软 Outlook OAuth2 授权与令牌管理器
使用标准 Device Code 流程获取持久 refresh_token，并提供自动刷新与 XOAUTH2 认证串生成。
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
SCOPES = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
TOKEN_FILE = os.path.join(CONFIG_DIR, "ms_tokens.json")

def get_opener():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or "http://127.0.0.1:7897"
    if proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "http://") # fallback if needed or let environment handle
    handlers = []
    if "127.0.0.1" in proxy:
        handlers.append(urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}))
    return urllib.request.build_opener(*handlers)

def request_device_code():
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": SCOPES
    }).encode("utf-8")
    req = urllib.request.Request(DEVICE_CODE_URL, data=data, method="POST")
    opener = get_opener()
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def poll_token(device_code: str, interval: int = 5, expires_in: int = 900):
    start = time.time()
    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    
    opener = get_opener()
    while time.time() - start < expires_in:
        time.sleep(interval)
        req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        try:
            with opener.open(req, timeout=15) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
                token_data["expires_at"] = int(time.time()) + token_data.get("expires_in", 3600)
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, indent=2)
                return token_data
        except urllib.error.HTTPError as e:
            res = json.loads(e.read().decode("utf-8"))
            err = res.get("error")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                interval += 5
                continue
            else:
                raise RuntimeError(f"OAuth2 认证失败: {res}")
    raise TimeoutError("Device code 等待授权超时")

def refresh_access_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        token_data = json.load(f)
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None
    
    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPES
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    opener = get_opener()
    with opener.open(req, timeout=15) as resp:
        new_data = json.loads(resp.read().decode("utf-8"))
        new_data["expires_at"] = int(time.time()) + new_data.get("expires_in", 3600)
        if "refresh_token" not in new_data:
            new_data["refresh_token"] = refresh_token
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)
        return new_data.get("access_token")

def get_valid_access_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        token_data = json.load(f)
    if time.time() < token_data.get("expires_at", 0) - 60:
        return token_data.get("access_token")
    return refresh_access_token()

def generate_xoauth2_string(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "token":
        tok = get_valid_access_token()
        if tok:
            print(tok)
            sys.exit(0)
        else:
            print("Token expired or not found", file=sys.stderr)
            sys.exit(1)
            
    info = request_device_code()
    print("DEVICE_CODE_INFO:" + json.dumps(info))
    tokens = poll_token(info["device_code"], info.get("interval", 5), info.get("expires_in", 900))
    print("OAUTH2_SUCCESS")
