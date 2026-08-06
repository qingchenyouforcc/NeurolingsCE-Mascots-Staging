# 安全策略

## 报告漏洞

请通过 GitHub Security Advisory 或 Issues 私信维护者；不要公开未修复漏洞。

## 威胁模型

| 威胁 | 缓解 |
| --- | --- |
| 恶意 `.mascot` 包 | `NeurolingsCE-cli --mascot validate`（路径/扩展名/大小/PNG/SHA-256），PR workflow 强制重跑 |
| 投稿滥用 | 服务端身份验证、速率限制、幂等键、重复 ID/版本检查、维护者审核 |
| token/密钥泄露 | GitHub App 私钥只在服务端；用户 token 只用于 `/user` 且不落库；日志统一脱敏 |
| 供应链 Action 投毒 | workflow 显式最小权限；第三方 Action 必须固定完整 commit SHA |
| 索引被篡改 | 仅受保护 `main` 分支触发发布；发布前重校验 SHA-256；HTTPS 分发 |

## 最小权限

- PR 检查 workflow：`contents: read`、`pull-requests: read`、`issues: read`。
- 发布 workflow：`contents: write`、`pull-requests: write`（发布 + Pages）。
- 投稿服务 GitHub App 权限：
  - Repository contents: Read & write（分支/PR）
  - Releases: Write（draft release/asset）
  - Metadata: Read（必需）
  - 不需要 Administration 或 Actions 权限。

## 密钥处理

- 客户端不包含任何 secret；GitHub App Client ID 可公开。
- 服务端私钥仅通过环境变量/Secret 提供，禁止提交到仓库。
- token、Authorization、Cookie 在日志中一律脱敏；见
  `submission-service/redact.py`。
