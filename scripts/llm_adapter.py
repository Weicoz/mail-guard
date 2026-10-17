#!/usr/bin/env python3
"""
mail-guard 统一大模型与 Harness 适配调度层
核心设计目标:
1. 免 Key 模式 (Harness Mode): 零配置 API Key，直接调用本机已登录或已配置的 AI Coding Agent CLI:
   - 官方预置原生支持: claude (Claude Code), codex (OpenAI Codex), kimi (Kimi Code), agy (Antigravity), dsh (DeepSeek Harness)
   - 常用扩展工具支持: cline, zcode, kiro, kilo
   - 自定义模板: LLM_HARNESS_CMD 支持 `{prompt}` 占位符或 stdin 管道
2. API Key 模式 (API Mode): 兼容 OpenAI / DeepSeek / 通义千问 / Ollama / OneAPI 等标准 REST 接口。
3. 自动探测 (Auto Mode): 有 Key 走 API，无 Key 优先自动探测本机可用 Harness；若均不可用，优雅回退至本地规则引擎 (0-Token、0-Crash)。
"""

import os
import sys
import json
import re
import shutil
import shlex
import subprocess
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, Tuple, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/mail-guard")

# 预置 Harness 配置表
HARNESS_PROFILES = {
    "claude": {
        "bin": "claude",
        "alt_names": ["claude-code"],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Claude Code",
        "desc": "Anthropic Claude Code CLI (-p 快速单次调用)"
    },
    "codex": {
        "bin": "codex",
        "alt_names": [],
        "default_args": ["exec", "--ephemeral"],
        "stdin_arg": None,
        "name": "OpenAI Codex",
        "desc": "OpenAI Codex CLI (exec --ephemeral 单次免会话调用)"
    },
    "kimi": {
        "bin": "kimi",
        "alt_names": ["kimi-code"],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Kimi Code",
        "desc": "Kimi Code CLI (-p 非交互式单次调用)"
    },
    "agy": {
        "bin": "agy",
        "alt_names": ["antigravity"],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Google Antigravity",
        "desc": "Google Antigravity CLI (-p print 模式单次调用)"
    },
    "dsh": {
        "bin": "dsh",
        "alt_names": [],
        "default_args": ["headless"],
        "stdin_arg": None,
        "name": "DeepSeek Harness",
        "desc": "DeepSeek Harness (dsh headless 单次任务输出)"
    },
    "cline": {
        "bin": "cline",
        "alt_names": ["cline-cli"],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Cline CLI",
        "desc": "Cline CLI (-p 快速调用)"
    },
    "zcode": {
        "bin": "zcode",
        "alt_names": [],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "ZCode CLI",
        "desc": "ZCode CLI (-p 快速调用)"
    },
    "kiro": {
        "bin": "kiro",
        "alt_names": [],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Kiro CLI",
        "desc": "Kiro CLI (-p 快速调用)"
    },
    "kilo": {
        "bin": "kilo",
        "alt_names": [],
        "default_args": ["-p"],
        "stdin_arg": None,
        "name": "Kilo CLI",
        "desc": "Kilo CLI (-p 快速调用)"
    }
}

# 自动探测优先级
AUTO_DETECT_PRIORITY = ["claude", "codex", "kimi", "agy", "dsh", "cline", "zcode", "kiro", "kilo"]

# 额外搜寻的二进制路径 (即使 launchd/cron 只有最小 PATH 也能找到)
SEARCH_DIRS = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.kimi-code/bin"),
    os.path.expanduser("~/.cargo/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin"
]

def find_env_file() -> Optional[str]:
    candidates = [
        os.path.join(CONFIG_DIR, "mail-guard.env"),
        os.path.join(PROJECT_ROOT, "mail-guard.env"),
        os.path.join(PROJECT_ROOT, "config", "mail-guard.env")
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def load_config_env() -> Dict[str, str]:
    env = {}
    env_file = find_env_file()
    if env_file:
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if line_s and not line_s.startswith("#") and "=" in line_s:
                        k, v = line_s.split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    # 合并进程环境中的覆盖
    for k in [
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "LLM_HARNESS", "LLM_HARNESS_CMD", "LLM_HARNESS_TIMEOUT",
        "DEEPSEEK_API_KEY"
    ]:
        if k in os.environ and os.environ[k]:
            env[k] = os.environ[k]
    return env

def find_harness_binary(bin_name: str, alt_names: List[str] = None) -> Optional[str]:
    """寻找可执行文件的绝对路径，确保在非交互/后台守护环境下可靠运行"""
    all_names = [bin_name] + (alt_names or [])
    for name in all_names:
        p = shutil.which(name)
        if p and os.path.exists(p) and os.access(p, os.X_OK):
            return p
        for d in SEARCH_DIRS:
            cand = os.path.join(d, name)
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                return cand
    return None

def get_available_harnesses() -> Dict[str, str]:
    """返回系统中所有检测到的可用 Harness 及其绝对路径"""
    found = {}
    for h_id, prof in HARNESS_PROFILES.items():
        bin_path = find_harness_binary(prof["bin"], prof.get("alt_names", []))
        if bin_path:
            found[h_id] = bin_path
    return found

def detect_default_harness() -> Optional[Tuple[str, str]]:
    """按优先级返回最佳可用的 (harness_id, binary_path)"""
    available = get_available_harnesses()
    for h_id in AUTO_DETECT_PRIORITY:
        if h_id in available:
            return h_id, available[h_id]
    return None

def clean_json_response(raw_text: str) -> Optional[Any]:
    """
    从模型或 Harness 输出中稳健解析出合法 JSON 结构 (dict 或 list)
    自动过滤:
    - ANSI 转义终端颜色代码
    - ```json ... ``` 代码块标记
    - 前后多余的说明文字、前置日志或 Hook 输出
    """
    if not raw_text or not isinstance(raw_text, str):
        return None

    # 1. 消除 ANSI 终端控制符
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_text).strip()

    # 2. 尝试直接反序列化
    try:
        return json.loads(text)
    except Exception:
        pass

    # 3. 剥离 Markdown 代码块
    md_matches = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.IGNORECASE)
    for cand in md_matches:
        try:
            return json.loads(cand.strip())
        except Exception:
            continue

    # 4. 贪婪提取最外层的 { ... } 或 [ ... ]
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')

    candidates = []
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        candidates.append(text[first_bracket:last_bracket + 1])

    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            # 容错：修复 JSON 尾随逗号 (trailing commas)
            cleaned = re.sub(r',\s*([}\]])', r'\1', cand)
            try:
                return json.loads(cleaned)
            except Exception:
                continue

    return None

def invoke_harness(
    harness_id: str,
    prompt: str,
    timeout: int = 60,
    custom_cmd: Optional[str] = None
) -> str:
    """
    调用本地 AI Coding Harness CLI 执行单次提示词推理并返回 stdout 文本
    """
    # 构造执行环境环境变量 (注入本地常用 bin 路径)
    env = os.environ.copy()
    env["PATH"] = ":".join(SEARCH_DIRS) + ":" + env.get("PATH", "")

    # 1. 自定义命令模板支持 (针对 zcode, kiro, kilo, cline 或任意自研 CLI)
    if custom_cmd or harness_id == "custom":
        cmd_template = custom_cmd or ""
        if not cmd_template:
            raise ValueError("未配置 LLM_HARNESS_CMD 自定义命令")
        if "{prompt}" in cmd_template:
            # 安全替换 prompt 占位符
            full_cmd = cmd_template.replace("{prompt}", shlex.quote(prompt))
            proc = subprocess.run(full_cmd, shell=True, env=env, capture_output=True, text=True, timeout=timeout)
        else:
            # 将 prompt 通过管道作为 stdin 输入
            proc = subprocess.run(cmd_template, shell=True, env=env, input=prompt, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"自定义 Harness 执行退出码异常 ({proc.returncode}): {proc.stderr.strip()[:200]}")
        return proc.stdout

    # 2. 预置标准化 Harness
    h_id = harness_id.lower().strip()
    profile = HARNESS_PROFILES.get(h_id)
    if not profile:
        # 支持别名搜索
        for k, v in HARNESS_PROFILES.items():
            if h_id in v.get("alt_names", []):
                profile = v
                h_id = k
                break

    if not profile:
        raise ValueError(f"未知的 Harness 名称: {harness_id}")

    bin_path = find_harness_binary(profile["bin"], profile.get("alt_names", []))
    if not bin_path:
        raise FileNotFoundError(f"未在系统中找到 Harness [{h_id}] 的可执行文件 ({profile['bin']})")

    cmd = [bin_path] + list(profile.get("default_args", [])) + [prompt]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"Harness [{h_id}] 执行退出码异常 ({proc.returncode}): {proc.stderr.strip()[:200]}")

    return proc.stdout

def invoke_api(
    prompt: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o-mini",
    timeout: int = 30
) -> str:
    """
    通过标准 HTTP REST API 发起调用
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices", [])
        if not choices:
            raise ValueError(f"API 返回数据未包含 choices: {data}")
        return choices[0].get("message", {}).get("content", "")

def call_llm_or_harness(
    prompt: str,
    expect_json: bool = True,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None
) -> Optional[Any]:
    """
    统一模型与 Harness 调度入口
    
    调度逻辑:
    1. LLM_PROVIDER="harness" -> 强制使用 Harness (不调用任何远程 API)
    2. LLM_PROVIDER="api"     -> 强制使用 REST API (校验 API Key)
    3. LLM_PROVIDER="auto"    -> 默认自动模式:
       - 若配置了 API KEY 且非空，优先走 API 模式 (快速、低延迟)
       - 若无 API KEY，自动切换至 Harness 模式并调用本机可用 agent
       - 若 API 模式调用失败，自动回退到 Harness
    4. 若调用成功且 expect_json=True，返回解析好的 dict/list；
    5. 若所有方式均不可用或失败，安全返回 None，由调用方平滑降级至本地规则引擎。
    """
    if env is None:
        env = load_config_env()

    provider = env.get("LLM_PROVIDER", "auto").lower().strip()
    api_key = env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY", "")
    base_url = env.get("LLM_BASE_URL", "https://api.deepseek.com/v1" if "deepseek" in (env.get("LLM_MODEL", "")).lower() else "https://api.openai.com/v1").strip()
    model = env.get("LLM_MODEL", "deepseek-chat" if "deepseek" in base_url else "gpt-4o-mini").strip()
    harness_target = env.get("LLM_HARNESS", "auto").strip()
    custom_cmd = env.get("LLM_HARNESS_CMD", "").strip()
    default_timeout = int(env.get("LLM_HARNESS_TIMEOUT", "60")) if env.get("LLM_HARNESS_TIMEOUT") else 60
    act_timeout = timeout or default_timeout

    def _execute_harness() -> Optional[Any]:
        # 确定调用的 harness 目标
        if custom_cmd or harness_target == "custom":
            try:
                raw = invoke_harness("custom", prompt, timeout=act_timeout, custom_cmd=custom_cmd)
                return clean_json_response(raw) if expect_json else raw
            except Exception as err:
                print(f"[mail-guard-llm] 自定义 Harness 执行失败 ({err})", file=sys.stderr)
                return None

        candidates = []
        if harness_target and harness_target != "auto":
            candidates = [harness_target]
        else:
            avail = get_available_harnesses()
            for h in AUTO_DETECT_PRIORITY:
                if h in avail:
                    candidates.append(h)

        for h in candidates:
            try:
                raw = invoke_harness(h, prompt, timeout=act_timeout)
                res = clean_json_response(raw) if expect_json else raw
                if res is not None:
                    return res
            except Exception as err:
                print(f"[mail-guard-llm] Harness [{h}] 调用失败或超时 ({err})", file=sys.stderr)
                continue
        return None

    def _execute_api() -> Optional[Any]:
        if not api_key:
            return None
        raw = invoke_api(prompt, api_key, base_url=base_url, model=model, timeout=min(act_timeout, 30))
        return clean_json_response(raw) if expect_json else raw

    # 1. 显式强制 Harness 模式
    if provider == "harness":
        return _execute_harness()

    # 2. 显式强制 API 模式
    if provider == "api":
        try:
            return _execute_api()
        except Exception as err:
            print(f"[mail-guard-llm] API 模式调用失败 ({err})", file=sys.stderr)
            return None

    # 3. 自动模式 (auto)
    # A. 优先尝试 API (若配置了 Key)
    if api_key:
        try:
            res = _execute_api()
            if res is not None:
                return res
        except Exception as err:
            print(f"[mail-guard-llm] API 调用失败 ({err})，尝试切换为本机 Harness", file=sys.stderr)

    # B. 回退或直接调用 Harness
    harness_res = _execute_harness()
    if harness_res is not None:
        return harness_res

    # C. 全部未命中或失败，返回 None
    return None

def get_harness_status_report() -> Dict[str, Any]:
    """生成大模型与 Harness 调度状态诊断报告"""
    env = load_config_env()
    provider = env.get("LLM_PROVIDER", "auto").strip()
    api_key = env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY", "")
    base_url = env.get("LLM_BASE_URL", "https://api.openai.com/v1").strip()
    model = env.get("LLM_MODEL", "gpt-4o-mini").strip()
    harness_target = env.get("LLM_HARNESS", "auto").strip()
    custom_cmd = env.get("LLM_HARNESS_CMD", "").strip()

    available = get_available_harnesses()
    default_h = detect_default_harness()

    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else ("已配置" if api_key else "未配置")

    active_route = "未知"
    if provider == "api":
        active_route = f"REST API (模型: {model})"
    elif provider == "harness":
        active_route = f"Harness: {harness_target if harness_target != 'auto' else (default_h[0] if default_h else '无可用')}"
    else:
        if api_key:
            active_route = f"API 优先 (模型: {model})，Harness 备用"
        elif default_h:
            active_route = f"Harness 自动驱动 (选中: {default_h[0]})"
        else:
            active_route = "本地纯规则引擎 (未检测到 Key 与可用 Harness)"

    return {
        "provider": provider,
        "api_key_configured": bool(api_key),
        "api_key_masked": masked_key,
        "base_url": base_url,
        "model": model,
        "harness_target": harness_target,
        "custom_cmd": custom_cmd,
        "available_harnesses": available,
        "default_harness": default_h[0] if default_h else None,
        "active_route": active_route
    }

if __name__ == "__main__":
    import time
    print("=" * 60)
    print("🤖 mail-guard 模型与 Harness 调度层自检")
    print("=" * 60)
    report = get_harness_status_report()
    print(f"当前模式 (LLM_PROVIDER): {report['provider']}")
    print(f"当前主用路由:             {report['active_route']}")
    print(f"API Key 状态:             {report['api_key_masked']}")
    print(f"检测到的本机 Harness:")
    for h_id, prof in HARNESS_PROFILES.items():
        p = report["available_harnesses"].get(h_id)
        tag = f"✅ {p}" if p else "❌ 未安装"
        print(f"  • {prof['name']:<20} ({h_id:<8}): {tag}")

    if report["default_harness"]:
        print(f"\n自动推荐优先 Harness: {report['default_harness']}")

    # 简单测试结构化提取
    if "--test" in sys.argv or "-t" in sys.argv:
        print("\n>> 正在执行端到端轻量提取测试...")
        t0 = time.time()
        test_prompt = """请从以下邮件中提取验证码与发件服务:
主题: 【GitHub】Your two-factor authentication code
正文: Verification code: 492015. Expire in 10 minutes.
输出合法 JSON: {"code": "验证码", "service": "发信机构", "expires_in": "有效时限"}
"""
        res = call_llm_or_harness(test_prompt, expect_json=True)
        cost = time.time() - t0
        print(f"测试耗时: {cost:.2f}s")
        print(f"提取结果: {res}")
        if res and res.get("code") == "492015":
            print("✅ 模型/Harness 结构化提取验证成功！")
        else:
            print("⚠️ 结构化提取未返回预期结果，但已安全返回并允许规则容灾。")
