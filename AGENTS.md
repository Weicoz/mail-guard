# mail-guard 工作规则与上下文导航

本项目为安全隐私优先的多邮箱管理与自动化系统。核心准则：**零远程加载**、**纯文本隔离**、**0-Token 闲置监听**、**只读状态无损**。

## 核心路径与环境边界

- **本地真实凭据 (严格隔离，禁止提交)**：`~/.config/mail-guard/accounts.json`
- **微软 OAuth2 令牌缓存**：`~/.config/mail-guard/ms_tokens.json`
- **后台守护日志**：`~/.config/mail-guard/mailguard.log`
- **系统自启守护 (launchd)**：`~/Library/LaunchAgents/com.weicoz.mailguard.plist`
  - 刷新重启服务必须使用：`launchctl kickstart -k gui/$(id -u)/com.weicoz.mailguard`
- **本地代理**：海外邮箱 (Gmail, Outlook) 强制走 `127.0.0.1:7897` (SOCKS5)。
- **Thunderbird 活跃配置**：`~/Library/Thunderbird/Profiles/<profile-id>.default-release/`
  - 安全配置源为 `user.js`；修改首选项必须同步重置 `session.json` 并清理 `startupCache`。

## Agent 常用执行命令

- **主动按需拉取邮件 (只读无损 PEEK)**：
  - 结构化 JSON 输出：`python3 scripts/query_mail.py --account all --limit 1 --json`
  - 关键词/发件人筛选：`python3 scripts/query_mail.py --keyword "<kw>" --from "<sender>"`
- **单封邮件提取与后壳测试**：`python3 scripts/handle_mail.py <account> INBOX <uid>`
- **微软 Token 手动刷新与检验**：`python3 scripts/ms_oauth2.py token`

## 运维与安全约束

1. **状态无损**：主动查询邮件必须使用 `BODY.PEEK[]` 与只读模式，不得将未读邮件标记为已读。
2. **零远程加载**：邮件解析仅提取 `text/plain` 正文，物理阻断任何针对外部资源的 HTTP/DNS 请求。
3. **网络命令超时**：执行网络探测或端口排查时，必须使用 `lsof -nP` 禁用反向 DNS 解析，所有 curl 命令必须带超时限制（如 `curl -m 5`）。
4. **自闭环交付**：修改 GUI 客户端或守护服务后，必须由 Agent 自行重载并验证前台/后台状态，严禁要求用户手动关标签或刷新。
5. **统一推送调度**：通知优先接入本机统一通知调度器 `harness-notify`（`~/.config/harness-notify/config.json`），保持与主会话 harness 推送渠道与路由一致。
