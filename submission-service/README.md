# 投稿服务

Python 3.11+（stdlib 优先）实现：

- `POST /v1/auth/github`：携带 GitHub user access token 换取 5～10 分钟
  有效的 submission session token（HMAC 签名，绑定 GitHub login/user id）。
- `POST /v1/submissions`：multipart 上传（流式落盘、大小限制、幂等键），
  只接受 `Authorization: Bearer <session token>`。
- `GET /v1/submissions/<id>`：状态查询（服务 token 或投稿者 token）。
- `DELETE /v1/submissions/<id>`：取消并清理 draft release/PR。
- `GET /healthz`：健康检查。

安全边界：

- 用户 token 只用于 `GET /user` 验证身份，不落库、不写日志、请求后释放。
- GitHub user token 不会出现在 multipart 上传中，也不会出现在 metadata 中。
- 官方仓库操作使用 GitHub App installation token（最小权限）。
- Publisher GitHub App 与桌面 Login App 完全分离：
  `GITHUB_PUBLISHER_APP_ID` / `GITHUB_PUBLISHER_INSTALLATION_ID` /
  `GITHUB_PUBLISHER_PRIVATE_KEY_PATH` 只存在于投稿服务端。
- 日志统一脱敏（`redact.py`）。
- `SUBMISSION_SESSION_SECRET` 生产必填（hex/base64，解码 ≥32 字节）；
  session token 固定 HMAC-SHA256，含 `iss`/`aud`/`sub`/`login`/
  `iat`/`nbf`/`exp`/`jti`，有效期 5～10 分钟；dev/test 未设置时生成
  临时密钥并打印 `WARNING: ephemeral submission session key; not
  suitable for production`。
- `SUBMISSION_ENV=production` 时 `VALIDATOR_CLI` 必填且启动自检通过；
  每次投稿调用 `NeurolingsCE-cli --mascot validate`，超时/崩溃/非法 JSON
  一律 fail closed，绝不降级为嵌入式检查。
- `development`/`test` 模式允许嵌入式回退检查（`package_checks.py`），
  并明确记录当前不是生产模式。
- 服务端自行生成 `submission/<id>-<version>` 分支并只写
  `mascots/<id>/manifest.json`；PR 创建后再次核对 changed files 白名单，
  越界自动关闭 PR、删除分支与 draft release。
- 所有权与更新权限以 GitHub numeric user ID 为准
  （`owner.userId`/`maintainerUserIds`）：新 ID 记录首版提交者为
  owner/initial maintainer；已有 ID 必须由 `maintainerUserIds` 成员提交
  且版本严格递增；numeric ID 集合增删需批准。login 仅用于展示：同一
  numeric ID 允许 GitHub 改名，服务端用 session 中的当前 login 同步
  刷新 `owner.login`、`authors[*].githubLogin` 与对应索引的
  `maintainers[*]`，提交者不能修改其他 numeric ID 的 login；旧格式
  （无 `maintainerUserIds`）的更新 fail closed，需管理员迁移。
- 幂等：稳定 submission id + 每步状态落盘；GitHub 超时后先查询真实状态
  再决定重试，重复提交返回原投稿；补偿只删除属于该投稿的 draft 资源。
- GitHub 429 尊重 `Retry-After`，5xx/超时有限重试，不无限重试。

部署与配置见 [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)。
