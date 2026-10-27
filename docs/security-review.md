# Pre-publication security review / 发布前安全检查

## Update publication check / 本次更新发布检查

2026-10-27：中英文 README、OpenFang 接入、Assistant 和浏览器验收说明已同步。

- 对待发布文件快照和本地 Git 历史执行 Gitleaks 8.30.1 脱敏扫描，均无发现。
- 对待发布文件执行本地已配置密钥的精确匹配，以及个人主目录路径、机器名和邮箱格式检查；未发现密钥或私人身份信息。邮箱格式命中仅为示例维护者地址与 URL 校验测试数据。
- `.env`、运行数据库、日志、截图、依赖和构建产物均不在本次提交中。仅保留公共仓库署名及仿真验收样例，不发布本机运行报告。
- 提交身份沿用仓库的 GitHub noreply 邮箱，不使用私人邮箱。只推送 `main`，不上传本地工具的检查点引用。
- 44 项 Python 测试与 TypeScript/Vite 构建通过。本次未重新执行依赖漏洞审计；下方漏洞审计结果属于初始发布时的历史记录。

此检查针对本次发布候选内容，不是对所有潜在隐私问题或安全漏洞的保证。

## Initial release review / 初始发布检查

Review date / 检查日期: 2026-10-24 (America/Los_Angeles).

## Summary / 结论

The publication candidate was checked for credentials, personal identifiers, tracked runtime data, unsafe credential transport, and known dependency vulnerabilities. No secret was detected in the staged publication snapshot. This is a scoped review, not a guarantee that all vulnerabilities are absent.

已检查待发布源码中的密钥、个人标识、运行数据、凭据传输及已知依赖漏洞。暂存区发布快照未检出密钥。本次检查有明确范围，不代表不存在任何安全问题。

## Findings and disposition / 发现与处理

1. **Low — personal information in documentation and commit metadata (resolved).** Replaced a machine-specific home path with portable clone instructions in `README.md` and `README.zh-CN.md`. Configured a GitHub noreply author address for this repository before its first commit. Global Git identity was not changed.
   **低风险，已处理：** 文档改用通用克隆路径；首次提交使用仓库级 GitHub 隐私邮箱，全局 Git 身份保持原配置。
2. **Medium — remote HTTP could expose a model credential (resolved).** `robot_debug_lab/config.py:15` validates the provider URL before requests are made; only HTTPS or loopback HTTP is accepted, with no embedded credentials, query, or fragment. The authorization header remains server-side in `robot_debug_lab/agents.py`, and provider error bodies are not returned as diagnostic errors. Endpoint rejection/acceptance tests were added.
   **中风险，已处理：** 模型地址要求远程 HTTPS，仅回环服务允许 HTTP，并拒绝 URL 内嵌凭据及查询串；新增验证测试。
3. **Medium — local history and API are accessible to other local processes (accepted scope limitation).** `robot_debug_lab/app.py:83` returns history and a browser session token. `scripts/start.sh:8` binds only to loopback. There is no multi-user authentication: do not publish this service to a network or tunnel. See `SECURITY.md`.
   **中风险，使用边界：** 本机其他进程可读取历史并调用接口，仅适用于单用户仿真，不适合直接联网部署。
4. **Low — exported reports can contain private task text (documented).** Task prompts and observations are intentionally persisted and exported. Committed acceptance samples were inspected and contain simulation data only. Users must review their own future exports before sharing.
   **低风险，已说明：** 导出报告保留任务文本；当前提交样例仅含仿真观测，用户分享后续报告前仍需人工检查。

## Verification / 验证

- Gitleaks 8.30.1: zero findings in the staged publication snapshot, with redacted reporting. Its downloaded release archive matched the release checksums.
- Exact-match check against the local configured model key: no match in tracked content. `.env` is ignored and has permission `600`.
- Personal-path/identifier checks: no machine home path or private account identifier in the publication candidate. Public GitHub owner attribution remains intentional.
- `.gitignore:1` excludes `.env`; `.gitignore:7` excludes runtime data. Credentials, logs, SQLite files, ROS build/install artifacts, dependencies, and local screenshots are excluded.
- `pip-audit`: 22 locked Python dependencies checked, zero reported known vulnerabilities.
- `npm audit`: zero reported known vulnerabilities in the frontend lockfile.
- 18 pytest cases passed; TypeScript checking and production frontend build passed.

以上扫描均针对本次待发布版本。依赖漏洞数据库、未来提交和运行数据会变化；发布者仍需在后续提交前重复检查。此检查未覆盖实体机器人、跨机器 ROS 网络或生产部署渗透测试。

## Repeating checks / 重复检查

Use Gitleaks with redaction against a clean tracked-file snapshot and against Git history; avoid scanning the whole working directory into public logs because it intentionally contains a local secret file. Review the exact files and commit author metadata before pushing. Re-run `npm --prefix web audit` and `pip-audit --disable-pip --no-deps -r requirements.lock` with current advisory databases.

使用 Gitleaks 的脱敏输出检查干净的跟踪文件快照和 Git 历史，不要将含本地密钥的整个工作目录扫描结果发布到公共日志。推送前复核文件及提交作者元数据，并重新运行依赖审计。
