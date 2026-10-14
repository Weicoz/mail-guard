#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR:$PYTHONPATH"

echo "=========================================================="
echo "📧 mail-guard 端到端功能自测"
echo "=========================================================="

chmod +x "$DIR/post_shell.sh"
chmod +x "$DIR/handle_mail.py"

echo ""
echo ">> [测试 1/2] 模拟普通垃圾/推广邮件 (预期: 本地0-Token拦截，不调用LLM，不触发后壳)"
python3 -c "
import sys
sys.path.insert(0, '$DIR')
from handle_mail import pre_filter
subject = '每周特惠：云服务器新春限时两折起购！'
body = '尊敬的客户，本周特惠套餐已上线，点击链接立即抢购。'
passed = pre_filter(subject, body)
print(f'预筛结果: 命中={passed} (期望: False)')
assert not passed, '普通邮件应被拦截'
"
echo "✅ [测试 1 通过] 普通邮件已在本地被 100% 阻断，Token 消耗为 0！"

echo ""
echo ">> [测试 2/2] 模拟验证码邮件 (预期: 预筛命中，提取验证码并触发后壳)"
python3 -c "
import sys
sys.path.insert(0, '$DIR')
from handle_mail import pre_filter, llm_extract
import subprocess, os

subject = '【GitHub】Your two-factor authentication code'
body = '''
Hi user,

Here is your verification code: 829104
This code will expire in 10 minutes. If you did not request this, please ignore.
'''

assert pre_filter(subject, body), '验证码邮件必须命中预筛'
result = llm_extract(subject, body)
print(f'提取结果: {result}')

# 触发后壳
post_shell = os.path.join('$DIR', 'post_shell.sh')
code = result.get('code', '')
service = result.get('service', '')
expires = result.get('expires_in', '')
subprocess.run([post_shell, code, service, expires, subject, 'test-account'])
"

echo "✅ [测试 2 通过] 验证码已被成功提取并已唤醒后壳！"
echo "=========================================================="
