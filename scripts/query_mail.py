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

# 引入项目自带的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import ms_oauth2
except ImportError:
    ms_oauth2 = None

try:
    import cache_manager
except ImportError:
    cache_manager = None

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

def parse_batch_fetch(fetch_data):
    """解析 IMAP 批量返回的 tuple 集合"""
    items = []
    for entry in fetch_data:
        if isinstance(entry, tuple) and len(entry) == 2:
            meta, raw_bytes = entry[0], entry[1]
            m_uid = re.search(rb'UID\s+(\d+)', meta)
            m_size = re.search(rb'RFC822\.SIZE\s+(\d+)', meta)
            uid = m_uid.group(1).decode("utf-8", errors="ignore") if m_uid else ""
            size = int(m_size.group(1)) if m_size else len(raw_bytes)
            items.append((uid, raw_bytes, size))
    return items

def process_raw_mail_item(acc_name, acc_cfg, args, uid, raw_bytes):
    """从原始邮件字节流中解析出结构化 mail_item 并同步沉淀热快照"""
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject", ""))
    from_addr = decode_mime_header(msg.get("From", ""))
    to_addr = decode_mime_header(msg.get("To", ""))
    date_str = decode_mime_header(msg.get("Date", ""))
    body = extract_plain_text(msg)

    code_info = extract_code_info(subject, body)
    body_display = body if args.full else (
        body if len(body) <= 1000 else (body[:1000] + "\n...[正文过长已截断，使用 --full 查看全文]...")
    )

    item = {
        "account": acc_name,
        "user": acc_cfg.get("user"),
        "mailbox": args.folder,
        "uid": str(uid),
        "date": date_str,
        "from": from_addr,
        "to": to_addr,
        "subject": subject,
        "body_plain": body_display,
        "extracted": code_info,
        "cached": False
    }

    # 同步回填本地 SQLite WAL 热快照 (0 额外网络，单次耗时 < 1ms)
    if cache_manager:
        cache_manager.save_mail(
            account=acc_name,
            mailbox=args.folder,
            uid=str(uid),
            subject=subject,
            body_plain=body,
            date_str=date_str,
            from_addr=from_addr,
            to_addr=to_addr,
            code=code_info.get("code"),
            service=code_info.get("service"),
            expires_in=code_info.get("expires_in"),
            link=code_info.get("link")
        )

    return item

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
        res, sel_data = mail.select(args.folder, readonly=not args.mark_read)
        if res != "OK" or not sel_data or not sel_data[0]:
            mail.logout()
            return results

        try:
            total_msgs = int(sel_data[0])
        except Exception:
            total_msgs = 0

        if total_msgs == 0:
            mail.logout()
            return results

        # 判定是否为“拉取最新 N 封且无复杂历史过滤”的极速场景
        is_simple_latest = (args.days == 0 or args.days >= 30) and not (args.keyword or args.from_filter or args.subject)

        if is_simple_latest:
            # 协议级 Sequence Range 切片: 砍掉 530ms 的 search ALL 与 28KB 无用传输
            start_seq = max(1, total_msgs - args.limit + 1)
            seq_set = f"{start_seq}:{total_msgs}"
            fetch_cmd = "(UID RFC822.SIZE RFC822)" if args.mark_read else "(UID RFC822.SIZE BODY.PEEK[])"
            res, fetch_data = mail.fetch(seq_set, fetch_cmd)
            if res == "OK" and fetch_data:
                parsed_items = parse_batch_fetch(fetch_data)
                parsed_items.reverse()  # 最新优先
                for uid, raw_bytes, size in parsed_items[:args.limit]:
                    item = process_raw_mail_item(acc_name, acc_cfg, args, uid, raw_bytes)
                    results.append(item)
        else:
            # 复杂过滤模式: 按条件进行 UID search，然后批量抓取候选集合
            search_criteria = []
            if args.days > 0:
                since_date = (datetime.now() - timedelta(days=args.days)).strftime("%d-%b-%Y")
                search_criteria.extend(["SINCE", since_date])
            if not search_criteria:
                search_criteria = ["ALL"]

            res, data = mail.uid("search", None, *search_criteria)
            if res != "OK" or not data[0]:
                mail.logout()
                return results

            uids = data[0].split()
            if not uids:
                mail.logout()
                return results

            scan_limit = args.limit * 5 if (args.keyword or args.from_filter or args.subject) else args.limit
            scan_uids = uids[-scan_limit:]
            scan_uids.reverse()

            need_hdr_filter = bool(args.from_filter or args.subject)
            candidate_uids = []

            # 若有发件人或主题过滤，先轻量批量抓取信头初筛
            if need_hdr_filter:
                uid_chunk = b",".join(scan_uids)
                res, hdr_data = mail.uid("fetch", uid_chunk, "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
                if res == "OK" and hdr_data:
                    parsed_hdrs = parse_batch_fetch(hdr_data)
                    for uid, raw_hdr, _ in parsed_hdrs:
                        hdr_msg = email.message_from_bytes(raw_hdr)
                        sub_pre = decode_mime_header(hdr_msg.get("Subject", ""))
                        from_pre = decode_mime_header(hdr_msg.get("From", ""))
                        if args.from_filter and args.from_filter.lower() not in from_pre.lower():
                            continue
                        if args.subject and args.subject.lower() not in sub_pre.lower():
                            continue
                        candidate_uids.append(uid.encode("utf-8"))
                        if len(candidate_uids) >= args.limit:
                            break
            else:
                candidate_uids = scan_uids[:args.limit]

            if candidate_uids:
                fetch_cmd = "(UID RFC822.SIZE RFC822)" if args.mark_read else "(UID RFC822.SIZE BODY.PEEK[])"
                uid_str = b",".join(candidate_uids)
                res, msg_data = mail.uid("fetch", uid_str, fetch_cmd)
                if res == "OK" and msg_data:
                    parsed_msgs = parse_batch_fetch(msg_data)
                    uid_to_msg = {uid: (raw, sz) for uid, raw, sz in parsed_msgs}
                    for u in candidate_uids:
                        u_str = u.decode("utf-8", errors="ignore")
                        if u_str in uid_to_msg:
                            raw, sz = uid_to_msg[u_str]
                            item = process_raw_mail_item(acc_name, acc_cfg, args, u_str, raw)
                            if args.keyword:
                                kw = args.keyword.lower()
                                if kw not in item["subject"].lower() and kw not in item["body_plain"].lower():
                                    continue
                            results.append(item)
                            if len(results) >= args.limit:
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
    parser.add_argument("--live", action="store_true", help="强制穿透缓存直连 IMAP 邮件服务器获取最新邮件 (默认优先读取秒级本地热快照)")
    
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

    # 1. Cache-First 极速热快照读取 (若未指定 --live 且未要求强制标已读)
    used_cache = False
    if not args.live and not args.mark_read and cache_manager:
        query_acc = args.account if args.account in accounts else ("all" if args.account == "all" else None)
        if query_acc:
            cached_items = cache_manager.query_cached_mails(
                account=query_acc,
                mailbox=args.folder,
                limit=args.limit,
                keyword=args.keyword,
                from_filter=args.from_filter,
                subject=args.subject,
                days=args.days,
                full=args.full
            )
            # 补齐 user 字段
            for it in cached_items:
                acc_info = accounts.get(it["account"], {})
                it["user"] = acc_info.get("user", "")

            # 若查单账号有结果，或查 all 有数据，直接作为结果
            if cached_items:
                all_results = cached_items
                used_cache = True

    # 2. 直连 IMAP 查询 (未命中缓存或指定 --live / --mark-read)
    if not all_results and not used_cache:
        if len(target_accounts) > 1:
            with ThreadPoolExecutor(max_workers=min(len(target_accounts), 4)) as executor:
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

    source_tag = " [来自本地极速热快照 < 5ms，加 --live 强制直连邮件服务器]" if used_cache else " [来自 IMAP 实时连接]"
    print(f"共查询到 {len(all_results)} 封邮件{source_tag}:")
    for item in all_results:
        print("=" * 80)
        code_str = ""
        ext = item.get("extracted", {})
        if ext.get("code"):
            code_str = f" | 🔑 验证码: [{ext['code']}] (服务: {ext.get('service')}, 时效: {ext.get('expires_in')})"
        elif ext.get("link"):
            code_str = f" | 🔗 确认链接: [{ext.get('service')}]"
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
