# Security & Privacy / 安全与隐私

## Supported scope / 支持范围

Robot Debug Lab is a local, single-user ROS 2 simulation tool. Use the provided startup script, which listens only on `127.0.0.1`. It has no multi-user authentication. The session token and Host checks protect browser interactions; they do not prevent another local process from reading task history or invoking the API. Do not expose the service through a tunnel, port forwarding, or reverse proxy.

本项目仅面向本机单用户仿真。启动脚本仅监听 `127.0.0.1`。会话令牌和 Host 检查不等于多用户认证，本机其他进程可以访问任务历史及控制接口。请勿通过隧道、端口转发或反向代理暴露服务。

## Credentials and data / 凭据与数据

- Put your own model key in local `.env`, permission `600`. Never put it in frontend configuration, prompts, issues, screenshots, or commits.
- Remote model endpoints require HTTPS; plaintext HTTP is accepted only for loopback servers. URLs with embedded credentials, query strings, or fragments are rejected.
- AI mode sends task text and simulated robot observations to the configured provider. Rules mode does not call a model.
- Run history, prompts, observations, and errors are stored locally in `.runtime/`. Exported reports may contain private task text: inspect them before sharing.
- `.gitignore` excludes runtime data and common credential files, but does not protect files explicitly force-added to Git or previously committed history.

- 密钥只保存在本地 `.env`，权限设为 `600`；不要放到前端配置、提示词、Issue、截图或提交中。
- 远程模型接口必须使用 HTTPS；HTTP 仅允许回环地址。不接受 URL 内嵌账号密码、查询串或片段。
- AI 模式会向配置的服务商发送任务文本和仿真观测；规则模式不调用模型。
- `.runtime/` 保存任务历史、提示词、观测和错误。分享导出报告前应人工检查私密文本。
- `.gitignore` 无法保护强制添加的文件，也无法移除已有提交历史中的密钥。

If a credential is exposed, revoke it at the provider first, replace the local value, restart the service, and then address any published history. Deleting a file or making a repository private does not invalidate a leaked key.

密钥一旦泄露，应先在服务商后台撤销，再更换本地配置并重启，最后处理已发布历史。删除文件或把仓库改为私有都不能使泄露密钥失效。

## Reporting / 问题报告

Use the repository's **Security → Report a vulnerability** option when available. If unavailable, open a minimal issue requesting a private reporting channel; do not include secrets, private logs, or exploit details in public issues. This small project does not promise a response SLA.

优先使用仓库的 **Security → Report a vulnerability**。若不可用，可开一个不含敏感信息的 Issue 请求私下联系渠道；请勿在公开 Issue 中粘贴密钥、私人日志或完整利用细节。本项目暂不承诺响应时限。
