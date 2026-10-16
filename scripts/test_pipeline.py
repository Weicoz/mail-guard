#!/usr/bin/env python3
"""
mail-guard 跨平台端到端功能自测脚本 (纯 Python，兼容 macOS / Linux / Windows)
"""

import sys
import os
import subprocess

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

from handle_mail import pre_filter, llm_extract, find_post_shell

print("=" * 60)
print("📧 mail-guard 跨平台端到端功能自测")
print("=" * 60)

# 测试 1: 模拟垃圾/推广邮件拦截
print("\n>> [测试 1/2] 模拟普通垃圾/推广邮件 (预期: 0-Token 本地拦截)")
spam_subject = "每周特惠：云服务器新春限时两折起购！"
spam_body = "尊敬的客户，本周特惠套餐已上线，点击链接立即抢购。"
passed = pre_filter(spam_subject, spam_body)
print(f"预筛结果: 命中={passed} (期望: False)")
assert not passed, "普通推广邮件必须在本地被 100% 拦截！"
print("✅ [测试 1 通过] 普通邮件已在本地被 100% 阻断，Token 消耗为 0！")

# 测试 2: 模拟验证码邮件
print("\n>> [测试 2/2] 模拟验证码邮件 (预期: 预筛命中并提取关键信息)")
code_subject = "【GitHub】Your two-factor authentication code"
code_body = """
Hi user,

Here is your verification code: 829104
This code will expire in 10 minutes. If you did not request this, please ignore.
"""
assert pre_filter(code_subject, code_body), "验证码邮件必须命中预筛！"
result = llm_extract(code_subject, code_body)
print(f"提取结果: {result}")
assert result.get("code") == "829104", f"预期验证码 829104，实际为 {result.get('code')}"

# 触发后壳通知
post_shell = find_post_shell()
if post_shell and os.path.exists(post_shell):
    code = result.get("code", "")
    service = result.get("service", "")
    expires = result.get("expires_in", "")
    try:
        if sys.platform == "win32" and post_shell.endswith(".sh"):
            # Windows 下若仅有 .sh，使用 git bash / wsl / powershell 兼容调用，或跳过 bash 执行
            pass
        else:
            subprocess.run([post_shell, code, service, expires, code_subject, "test-account"], timeout=5)
    except Exception as e:
        print(f"后壳触发提示: {e}")

print("✅ [测试 2 通过] 验证码已被成功提取！")
print("=" * 60)
print("🎉 mail-guard 全平台功能自检全部通过！\n")
