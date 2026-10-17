#!/usr/bin/env python3
"""
mail-guard: Agent 主动邮件查询 CLI 工具 (query_mail.py)

功能:
1. 随时随地按账号 (qq, gmail1, gmail2, outlook, 或 all) 主动拉取邮件。
2. 支持按最新 N 封、发件人、主题、关键词、时间范围筛选。
3. 纯文本物理隔离: 绝不发起任何外部资源请求，不加载远程图片与追踪像素。
4. 状态无损: 默认以只读 PEEK 模式读取，绝不改变邮件的已读/未读状态。
5. 专为 Agent 设计: 提供 --json 结构化输出，自动结构化提炼验证码/安全提醒。
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
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")
ACCOUNTS_CONFIG = os.path.join(CONFIG_DIR, "accounts.json")

# 引入项目自带的 OAuth2 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import ms_oauth2
except ImportError:
    ms_oauth2 = None

# 验证码快速正则
CODE_REGEX = re.compile(r'(?:验证码|动态码|code|Code|OTP|passcode)[^\d\w]{0,10}([0-9a-zA-Z]{4,8})\b')
FALLBACK_CODE_REGEX = re.compile(r'\b([0-9]{4,6})\b')
EXPIRES_REGEX = re.compile(r'([0-9一二两三四五六七八九十]+(?:分钟|小时|秒|min|minute|hour)内?(?:有效)?)')
SERVICE_REGEX = re.compile(r'【([^】]+)】|\[([^\]]+)\]')
LINK_REGEX = re.compile(
    r'(https?://[^\s<>"\'\)]*(?:magic|token|login|signin|session|verify|confirm|activate|auth|reset)[^\s<>"\'\)]*)',
    re.IGNORECASE
)

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

def html_to_safe_plain_text(html_content: str) -> str:
    """纯离线安全解析，阻断任何外链与间谍像素，提取纯文本与超链接"""
    if not html_content:
        return ""
    cleaned = re.sub(r'(?is)<(script|style|head|noscript)[^>]*>.*?</\1>', '', html_content)
    cleaned = re.sub(r'(?i)<a\s+[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', cleaned)
    cleaned = re.sub(r'(?i)<(p|div|br|tr|li|h[1-6])[^>]*>', '\n', cleaned)
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    cleaned = cleaned.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()

def extract_plain_text(msg):
    """提取纯文本；若无 text/plain 则离线安全净化 text/html，杜绝外部加载并保全正文"""
    plain_parts = []
    html_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get('Content-Disposition'))
            if 'attachment' in cdisp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or 'utf-8'
            text = payload.decode(charset, errors='ignore')
            if ctype == 'text/plain':
                plain_parts.append(text)
            elif ctype == 'text/html' and not plain_parts:
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        raw = payload.decode(charset, errors='ignore') if payload else ""
        if msg.get_content_type() == 'text/plain':
            plain_parts.append(raw)
        elif msg.get_content_type() == 'text/html':
            html_parts.append(raw)

    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        return html_to_safe_plain_text("\n".join(html_parts))
    return ""

def extract_code_info(subject, body):
    text = f"{subject}\n{body}"
    code_match = CODE_REGEX.search(text)
    if not code_match:
        code_match = FALLBACK_CODE_REGEX.search(text)
    code = code_match.group(1) if code_match else None

    exp_match = EXPIRES_REGEX.search(text)
    expires = exp_match.group(1) if exp_match else "尽快使用"

    srv_match = SERVICE_REGEX.search(subject)
    if srv_match:
        service = srv_match.group(1) or srv_match.group(2)
    else:
        service = subject.split()[0] if subject else "未知平台"

    link_match = LINK_REGEX.search(text)
    link = link_match.group(1) if link_match else None

    return {"code": code, "service": service, "expires_in": expires, "link": link}

def load_accounts():
    if not os.path.exists(ACCOUNTS_CONFIG):
        raise FileNotFoundError(f"配置文件未找到: {ACCOUNTS_CONFIG}")
    with open(ACCOUNTS_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)

def connect_imap(acc_name, acc_cfg, mark_read=False):
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

    if hasattr(mail, "sock") and mail.sock:
        try:
            mail.sock.settimeout(12)
        except Exception:
            pass

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

def query_account_mails(acc_name, acc_cfg, args):
    results = []
    try:
        mail = connect_imap(acc_name, acc_cfg, mark_read=args.mark_read)
    except Exception as e:
        if not args.json:
            print(f"[{acc_name}] 连接失败: {e}", file=sys.stderr)
        return results

    try:
        # 默认使用只读模式，不改变服务器上邮件状态
        mail.select(args.folder, readonly=not args.mark_read)

        # 构造搜索条件
        search_criteria = ["ALL"]
        if args.days > 0:
            since_date = (datetime.now() - timedelta(days=args.days)).strftime("%d-%b-%Y")
            search_criteria = ["SINCE", since_date]

        res, data = mail.uid("search", None, *search_criteria)
        if res != "OK" or not data[0]:
            mail.logout()
            return results

        uids = data[0].split()
        if not uids:
            mail.logout()
            return results

        # 倒序遍历（从最新到最旧）
        # 如果有关键词或发件人过滤，多扫描一部分候选邮件
        scan_limit = args.limit * 5 if (args.keyword or args.from_filter or args.subject) else args.limit
        scan_uids = uids[-scan_limit:]
        scan_uids.reverse()

        need_hdr_filter = bool(args.from_filter or args.subject)
        count = 0
        for uid in scan_uids:
            # 阶段 1: 若指定发件人或主题过滤，先抓取信头初筛，避免无意义下载大邮件的正文
            if need_hdr_filter:
                res, hdr_data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
                if res != "OK" or not hdr_data or not hdr_data[0] or not isinstance(hdr_data[0], tuple):
                    continue
                raw_hdr = hdr_data[0][1]
                hdr_msg = email.message_from_bytes(raw_hdr)
                subject_pre = decode_mime_header(hdr_msg.get("Subject", ""))
                from_pre = decode_mime_header(hdr_msg.get("From", ""))
                if args.from_filter and args.from_filter.lower() not in from_pre.lower():
                    continue
                if args.subject and args.subject.lower() not in subject_pre.lower():
                    continue

            fetch_cmd = "(RFC822)" if args.mark_read else "(BODY.PEEK[])"
            res, msg_data = mail.uid("fetch", uid, fetch_cmd)
            if res != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                continue

            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue

            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", ""))
            from_addr = decode_mime_header(msg.get("From", ""))
            to_addr = decode_mime_header(msg.get("To", ""))
            date_str = decode_mime_header(msg.get("Date", ""))
            body = extract_plain_text(msg)

            # 过滤逻辑 (Python 本地过滤，兼容全字符集与中文)
            if args.from_filter and args.from_filter.lower() not in from_addr.lower():
                continue
            if args.subject and args.subject.lower() not in subject.lower():
                continue
            if args.keyword:
                kw = args.keyword.lower()
                if kw not in subject.lower() and kw not in body.lower():
                    continue

            code_info = extract_code_info(subject, body)
            
            # 正文展示截断（除非指定 --full）
            body_display = body if args.full else (body[:1000] + ("\n...[正文过长已截断，使用 --full 查看全文]..." if len(body) > 1000 else ""))

            mail_item = {
                "account": acc_name,
                "user": acc_cfg.get("user"),
                "mailbox": args.folder,
                "uid": uid.decode("utf-8", errors="ignore"),
                "date": date_str,
                "from": from_addr,
                "to": to_addr,
                "subject": subject,
                "body_plain": body_display,
                "extracted": code_info
            }
            results.append(mail_item)
            count += 1
            if count >= args.limit:
                break

        mail.logout()
    except Exception as e:
        if not args.json:
            print(f"[{acc_name}] 查询邮件发生异常: {e}", file=sys.stderr)

    return results

def main():
    parser = argparse.ArgumentParser(description="mail-guard: Agent 邮件主动查询工具")
    parser.add_argument("--account", "-a", default="all", help="要查询的邮箱账号 (qq, gmail1, gmail2, outlook, 或 all，默认 all)")
    parser.add_argument("--limit", "-n", type=int, default=1, help="每个账号返回的最大邮件数量 (默认: 1)")
    parser.add_argument("--keyword", "-k", default="", help="正文或主题关键词过滤")
    parser.add_argument("--from", "-f", dest="from_filter", default="", help="发件人过滤 (地址或名称)")
    parser.add_argument("--subject", "-s", default="", help="主题关键词过滤")
    parser.add_argument("--days", "-d", type=int, default=30, help="查询最近 N 天内的邮件 (默认: 30，0 表示不限时间)")
    parser.add_argument("--folder", default="INBOX", help="IMAP 文件夹名 (默认: INBOX)")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出 (专供 Agent 解析)")
    parser.add_argument("--full", action="store_true", help="输出完整邮件正文 (不截断)")
    parser.add_argument("--mark-read", action="store_true", help="将查询到的邮件标记为已读 (默认保持未读 PEEK 状态)")
    
    args = parser.parse_args()

    accounts = load_accounts()
    target_accounts = {}
    if args.account == "all":
        target_accounts = accounts
    elif args.account in accounts:
        target_accounts = {args.account: accounts[args.account]}
    else:
        # 支持按邮箱地址匹配
        matched = False
        for k, v in accounts.items():
            if v.get("user") == args.account:
                target_accounts = {k: v}
                matched = True
                break
        if not matched:
            print(f"错误: 未找到账号 '{args.account}'。可用账号: {list(accounts.keys())}", file=sys.stderr)
            sys.exit(1)

    all_results = []
    if len(target_accounts) > 1:
        with ThreadPoolExecutor(max_workers=min(len(target_accounts), 8)) as executor:
            future_to_acc = {
                executor.submit(query_account_mails, acc_name, acc_cfg, args): acc_name
                for acc_name, acc_cfg in target_accounts.items()
            }
            for future in as_completed(future_to_acc):
                acc_name = future_to_acc[future]
                try:
                    mails = future.result()
                    all_results.extend(mails)
                except Exception as e:
                    if not args.json:
                        print(f"[{acc_name}] 查询线程异常: {e}", file=sys.stderr)
    else:
        for acc_name, acc_cfg in target_accounts.items():
            mails = query_account_mails(acc_name, acc_cfg, args)
            all_results.extend(mails)

    def parse_date_epoch(date_str):
        try:
            return parsedate_to_datetime(date_str).timestamp()
        except Exception:
            return 0.0

    all_results.sort(key=lambda x: parse_date_epoch(x.get("date", "")), reverse=True)

    if args.json:
        print(json.dumps(all_results, ensure_ascii=False, indent=2))
        return

    # 人性化/Agent 紧凑文本输出
    if not all_results:
        print(f"未找到符合条件的邮件 (账号: {args.account}, 关键词: '{args.keyword}', 发件人: '{args.from_filter}')")
        return

    print(f"共查询到 {len(all_results)} 封邮件:")
    for item in all_results:
        print("=" * 80)
        code_str = ""
        ext = item["extracted"]
        if ext.get("code"):
            code_str = f" | 🔑 验证码: [{ext['code']}] (服务: {ext['service']}, 时效: {ext['expires_in']})"
        elif ext.get("link"):
            code_str = f" | 🔗 确认链接: [{ext['service']}]"
        print(f"[{item['account']}] UID: {item['uid']} | 日期: {item['date']}{code_str}")
        print(f"发件人: {item['from']}")
        print(f"主题: {item['subject']}")
        if ext.get("link"):
            print(f"链接地址: {ext['link']}")
        print("-" * 80)
        print("纯文本正文:")
        print(item["body_plain"])
    print("=" * 80)

if __name__ == "__main__":
    main()
