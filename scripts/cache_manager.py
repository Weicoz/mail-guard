#!/usr/bin/env python3
"""
mail-guard: 轻量本地快照缓存管理器 (cache_manager.py)

功能:
1. SQLite WAL 模式极速热缓存，单次读写 < 5ms。
2. 磁盘极低磨损: 顺序极小追加，单条记录 < 1KB，正文截断 2000 字符。
3. 惰性淘汰: 每个账号固定保留最新 30 封，超出 35 封批量修剪旧记录。
4. 专为 Agent 秒级查询设计，Cache-First 命中即返回，0 额外网络与 Token 开销。
"""

import os
import sys
import time
import sqlite3
import re
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

CACHE_DIR = os.path.expanduser("~/.config/mail-guard/cache")
DB_PATH = os.path.join(CACHE_DIR, "mails.db")

# 正则表达式 (0-Token 快速提取)
CODE_REGEX = re.compile(r'(?:验证码|动态码|code|Code|OTP|passcode)[^\d\w]{0,10}([0-9a-zA-Z]{4,8})\b')
FALLBACK_CODE_REGEX = re.compile(r'\b([0-9]{4,6})\b')
EXPIRES_REGEX = re.compile(r'([0-9一二两三四五六七八九十]+(?:分钟|小时|秒|min|minute|hour)内?(?:有效)?)')
SERVICE_REGEX = re.compile(r'【([^】]+)】|\[([^\]]+)\]')
LINK_REGEX = re.compile(
    r'(https?://[^\s<>"\'\)]*(?:magic|token|login|signin|session|verify|confirm|activate|auth|reset)[^\s<>"\'\)]*)',
    re.IGNORECASE
)

def parse_date_epoch(date_str: str) -> float:
    if not date_str:
        return time.time()
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.timestamp()
    except Exception:
        return time.time()

def extract_code_info_fast(subject: str, body: str) -> Dict[str, str]:
    """0-Token 正则快速提取验证码、服务名、有效时间与登录链接"""
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
        srv_match = SERVICE_REGEX.search(body[:300])
        service = (srv_match.group(1) or srv_match.group(2)) if srv_match else "未知服务"

    link_match = LINK_REGEX.search(body)
    link = link_match.group(1) if link_match else None

    return {
        "code": code,
        "service": service,
        "expires_in": expires,
        "link": link
    }

def get_connection() -> sqlite3.Connection:
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn

def init_db():
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS mails (
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid TEXT NOT NULL,
                date_epoch REAL NOT NULL,
                date_str TEXT,
                from_addr TEXT,
                to_addr TEXT,
                subject TEXT,
                body_plain TEXT,
                code TEXT,
                service TEXT,
                expires_in TEXT,
                link TEXT,
                cached_at REAL NOT NULL,
                PRIMARY KEY (account, mailbox, uid)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_lookup ON mails(account, mailbox, date_epoch DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_epoch ON mails(date_epoch DESC);")
    finally:
        conn.close()

def save_mail(
    account: str,
    mailbox: str,
    uid: str,
    subject: str,
    body_plain: str,
    date_str: str = "",
    from_addr: str = "",
    to_addr: str = "",
    date_epoch: Optional[float] = None,
    code: Optional[str] = None,
    service: Optional[str] = None,
    expires_in: Optional[str] = None,
    link: Optional[str] = None
) -> None:
    """写入单封邮件至快照缓存，自动截断正文并维护最新上限"""
    init_db()
    
    # 截断正文至 2000 字符，消除大正文与多余磁盘占用
    safe_body = body_plain[:2000] if body_plain else ""
    
    # 补齐提取信息
    if code is None or service is None or link is None:
        extracted = extract_code_info_fast(subject, safe_body)
        code = code or extracted["code"]
        service = service or extracted["service"]
        expires_in = expires_in or extracted["expires_in"]
        link = link or extracted["link"]

    if date_epoch is None:
        date_epoch = parse_date_epoch(date_str)

    now = time.time()
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
            INSERT OR REPLACE INTO mails (
                account, mailbox, uid, date_epoch, date_str,
                from_addr, to_addr, subject, body_plain,
                code, service, expires_in, link, cached_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                account, mailbox, str(uid), date_epoch, date_str,
                from_addr, to_addr, subject, safe_body,
                code, service, expires_in, link, now
            ))
            
            # 惰性淘汰: 检查该邮箱总邮件数，超过 35 封时修剪至 30 封
            cur = conn.execute("SELECT COUNT(*) FROM mails WHERE account = ? AND mailbox = ?;", (account, mailbox))
            count = cur.fetchone()[0]
            if count > 35:
                conn.execute("""
                DELETE FROM mails WHERE account = ? AND mailbox = ? AND uid NOT IN (
                    SELECT uid FROM mails WHERE account = ? AND mailbox = ? ORDER BY date_epoch DESC LIMIT 30
                );
                """, (account, mailbox, account, mailbox))
    except Exception as e:
        print(f"[cache_manager] 保存邮件失败 ({account}:{uid}): {e}", file=sys.stderr)
    finally:
        conn.close()

def update_mail_extraction(
    account: str,
    mailbox: str,
    uid: str,
    code: str,
    service: str,
    expires_in: str,
    link: str
) -> None:
    """大模型提取完成后增量更新结构化信息"""
    init_db()
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
            UPDATE mails SET code = ?, service = ?, expires_in = ?, link = ?, cached_at = ?
            WHERE account = ? AND mailbox = ? AND uid = ?;
            """, (code, service, expires_in, link, time.time(), account, mailbox, str(uid)))
    except Exception as e:
        print(f"[cache_manager] 更新结构化提取失败 ({account}:{uid}): {e}", file=sys.stderr)
    finally:
        conn.close()

def query_cached_mails(
    account: str = "all",
    mailbox: str = "INBOX",
    limit: int = 1,
    keyword: str = "",
    from_filter: str = "",
    subject: str = "",
    days: int = 30,
    full: bool = False
) -> List[Dict[str, Any]]:
    """从本地 SQLite WAL 快照极速查询，耗时通常 < 5ms"""
    if not os.path.exists(DB_PATH):
        return []

    conditions = []
    params = []

    if account != "all":
        conditions.append("account = ?")
        params.append(account)

    if mailbox:
        conditions.append("mailbox = ?")
        params.append(mailbox)

    if days > 0:
        since_epoch = time.time() - (days * 86400)
        conditions.append("date_epoch >= ?")
        params.append(since_epoch)

    if from_filter:
        conditions.append("LOWER(from_addr) LIKE ?")
        params.append(f"%{from_filter.lower()}%")

    if subject:
        conditions.append("LOWER(subject) LIKE ?")
        params.append(f"%{subject.lower()}%")

    if keyword:
        conditions.append("(LOWER(subject) LIKE ? OR LOWER(body_plain) LIKE ?)")
        params.append(f"%{keyword.lower()}%")
        params.append(f"%{keyword.lower()}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    # 若全局查且 limit 较小，按账号分组取最新，或者直接全局按时间倒序
    # 此处按账号取最新 limit 封或全局倒序切片
    query_sql = f"""
    SELECT account, mailbox, uid, date_epoch, date_str,
           from_addr, to_addr, subject, body_plain,
           code, service, expires_in, link, cached_at
    FROM mails
    {where_clause}
    ORDER BY date_epoch DESC
    """

    conn = get_connection()
    results = []
    try:
        cur = conn.execute(query_sql, params)
        rows = cur.fetchall()
        
        # 统计每个账号取 limit 封
        acc_counts = {}
        for row in rows:
            acc = row[0]
            if account == "all":
                c = acc_counts.get(acc, 0)
                if c >= limit:
                    continue
                acc_counts[acc] = c + 1
            else:
                if len(results) >= limit:
                    break

            body_raw = row[8] or ""
            body_display = body_raw if full else (
                body_raw if len(body_raw) <= 1000 else (body_raw[:1000] + "\n...[正文过长已截断，使用 --full 查看全文]...")
            )

            item = {
                "account": row[0],
                "mailbox": row[1],
                "uid": row[2],
                "date": row[4],
                "date_epoch": row[3],
                "from": row[5] or "",
                "to": row[6] or "",
                "subject": row[7] or "",
                "body_plain": body_display,
                "extracted": {
                    "code": row[9],
                    "service": row[10],
                    "expires_in": row[11],
                    "link": row[12]
                },
                "cached": True,
                "cached_at": row[13]
            }
            results.append(item)
    except Exception as e:
        print(f"[cache_manager] 查询缓存失败: {e}", file=sys.stderr)
    finally:
        conn.close()

    return results

if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)
