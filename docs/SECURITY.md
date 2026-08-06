# 安全策略

## 报告漏洞

请通过 GitHub Security Advisory 或 Issues 私信维护者；不要公开未修复漏洞。

## 威胁模型

| 威胁 | 缓解 |
| --- | --- |
| 恶意 `.mascot` 包 | `NeurolingsCE-cli --mascot validate`（路径/扩展名/大小/PNG/SHA-256），PR workflow 强制重跑 |
| 投稿滥用 | 服务端身份验证、速率限制、幂等键、重复 ID/版本检查、维护者审核 |
| token/密钥泄露 | Publisher App 私钥只在服务端；Login App 只用于 Device Flow；用户 token 只用于换取 session token；日志统一脱敏 |
| 供应链 Action 投毒 | workflow 显式最小权限；第三方 Action 固定完整 commit SHA |
| 未经审核的包提前公开 | Draft Release 保持 draft；PR validation 使用只读 `GITHUB_TOKEN` 认证下载 Draft asset，禁止匿名下载与提前发布 |
| 索引被篡改 | 仅受保护 `main` 分支触发发布；发布前重校验 SHA-256；索引由已发布 Release 状态推导 |

## 两个 GitHub App

- **Login App**：桌面 Device Flow，权限全部 None，只读 `GET /user`；
  设备码请求只发送 `client_id`，不发送 OAuth `scope`。客户端只包含
  Login App 的 Client ID。
- **Publisher App**：只运行在投稿服务端，Repository permissions 为
  Metadata Read-only、Contents Read and write、Pull requests Read and
  write；私钥只在服务端，客户端永远拿不到 Publisher App 的 user token。

## Session token

- `POST /v1/auth/github` 用 GitHub user token 换取 5～10 分钟有效的
  submission session token（HMAC-SHA256 固定算法，含 `iss`/`aud`/`sub`/
  `login`/`iat`/`nbf`/`exp`/`jti`）。
- 生产模式 `SUBMISSION_SESSION_SECRET` 必填：hex 或 base64，解码后 ≥32
  字节；缺失/过短/非法编码拒绝启动，不生成临时密钥。
- 签名使用常量时间比较；拒绝未知算法、错误 audience/issuer、过期或
  未生效 token。

## Workflow 权限与 Secrets 表述

区分两类凭据：

- **自动生成的 `GITHUB_TOKEN`**：GitHub Actions 为每个 job 自动生成的
  仓库范围 installation token，权限由 workflow `permissions` 决定，不是
  维护者手动配置的 repository secret。
- **配置的 repository/environment Secrets**：维护者手动设置的高权限
  凭据（例如 Publisher App 私钥）。当前所有 workflow **都不使用**这类
  Secrets。

各 workflow 只使用自动 `GITHUB_TOKEN`：

- PR 验证：`contents: read` + `pull-requests: read`，仅用于 API 读取、
  认证下载 Draft asset；
- 发布与部署（合并 workflow `publish-and-deploy.yml`）：
  - `publish_releases`：`contents: write`，用于 PATCH Release；
  - `generate_index`：`contents: read`，仅用于读取已发布 tag；
  - `deploy_pages`：`pages: write` + `id-token: write`；
  - job 顺序固定，任一失败即停止，不部署不完整索引；
  - concurrency group 固定为 `publish-and-deploy` 且 `queue: max`
    （无 `cancel-in-progress`）：新 push 不会替换 pending 运行；执行
    顺序不保证等于 push 顺序，每次运行都按执行时真实 Release 状态
    幂等收敛。
- 清理：`contents: write` + `pull-requests: read`，用于核对并删除
  验证过的 draft release。

PR 验证的信任边界：

- checkout 固定 `pull_request.base.sha`，不 checkout PR head；
- 不执行 PR 分支中的任何 Python/shell/workflow/CMake/可执行文件；
- changed files、manifest、release、asset 全部通过 GitHub API 读取并
  白名单校验；
- Draft asset 只通过 Release Asset API 认证下载，token 不传给验证器、
  不打印 Authorization；
- 验证失败即失败，绝不发布 Release。

## 发布状态与所有权

- 源 manifest 不保存可推导的发布状态；GitHub Release 是唯一发布状态来源，
  `index-v1.json` 写入派生 `status: published`。
- 权限依据是 GitHub numeric user ID（`owner.userId`/`maintainerUserIds`）；
  login 改名不丢失权限，login 相同但 numeric ID 不同不获得权限；
  修改 maintainers/owner 必须经现有 maintainer 或管理员批准。

## 密钥处理

- 客户端不包含任何 secret；Login App Client ID 可公开。
- Publisher App 私钥仅通过环境变量/Secret 提供，禁止提交到仓库。
- token、Authorization、Cookie 在日志中一律脱敏；见
  `submission-service/redact.py`。
