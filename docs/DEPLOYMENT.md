# 投稿服务部署

服务为 Python 3.11+ **stdlib 优先**实现，唯一可选依赖是
`cryptography`（GitHub App JWT RS256 签名，Apache-2.0/MIT 双许可）；
没有 `cryptography` 时会尝试系统 `openssl`，两者都不可用则启动失败并给出明确错误。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUBMISSION_PORT` | `8000` | HTTP 端口 |
| `SUBMISSION_STORAGE_DIR` | `./data` | 投稿状态目录 |
| `SUBMISSION_BASE_URL` | `http://localhost:8000` | 用于生成回显 URL |
| `GITHUB_PUBLISHER_APP_ID` | — | **Publisher** GitHub App ID（区别于 Login App） |
| `GITHUB_PUBLISHER_INSTALLATION_ID` | — | Publisher App 安装 ID |
| `GITHUB_PUBLISHER_PRIVATE_KEY_PATH` | — | Publisher App PEM 文件路径 |
| `GITHUB_OWNER` / `GITHUB_REPO` | `qingchenyouforcc` / `NeurolingsCE-Mascots` | 官方仓库 |
| `MAX_UPLOAD_BYTES` | `104857600`（100 MiB） | 包大小上限 |
| `RATE_LIMIT_SUBMISSIONS_PER_MINUTE` | `10` | 每用户/每 IP 速率限制 |
| `SUBMISSION_AUTH_RATE_LIMIT_PER_MINUTE` | `30` | 认证端点每用户/每 IP 速率限制 |
| `SUBMISSION_SERVICE_TOKEN` | — | 可选 Bearer 服务认证（不作为安全边界） |
| `SUBMISSION_SESSION_SECRET` | 仅 dev/test 临时密钥 | 生产**必填**：hex 或 base64，解码后 ≥32 字节；生产缺失/过短/非法编码直接拒绝启动 |
| `SUBMISSION_SESSION_TTL_SECONDS` | `600` | session token 有效期（严格 300～600 秒） |
| `SUBMISSION_ENV` | `development` | `development` / `test` / `production` |
| `VALIDATOR_CLI` | — | **生产模式必须配置**：`NeurolingsCE-cli` 可执行文件路径 |
| `VALIDATOR_TIMEOUT_SECONDS` | `120` | 验证器超时（超时即 fail closed） |

## 启动

```bash
python -m pip install -r submission-service/requirements.txt
export GITHUB_PUBLISHER_PRIVATE_KEY_PATH=/run/secrets/publisher-app-key.pem
export SUBMISSION_ENV=production
export SUBMISSION_SESSION_SECRET="$(openssl rand -hex 32)"
python submission-service/app.py
```

生产建议：放在反向代理（HTTPS）之后，限制请求体大小，
并将 `SUBMISSION_STORAGE_DIR` 挂到持久卷。
完整变量清单示例见 `submission-service/env.example`（禁止提交真实值）。

## 生产模式自检

`SUBMISSION_ENV=production` 时服务启动会：

1. 校验 `SUBMISSION_SESSION_SECRET` 已配置、可解码（hex/base64）且 ≥32 字节；
2. 校验 `VALIDATOR_CLI` 已配置、文件存在、可执行；
3. 运行内置最小有效包对验证器做自检（退出码 0 + 合法 JSON）；
4. 任一项失败直接拒绝启动。

每次投稿都会调用公共验证器；超时、崩溃、非法 JSON 均 fail closed，
不会降级为 Python 轻量检查。

## 健康检查

`GET /healthz` 返回 `{"ok":true}`，不依赖 GitHub。

## 发布与 main 分支保护

- 发布与 Pages 部署合并为单个 workflow `publish-and-deploy.yml`：
  `publish_releases → generate_index → deploy_pages`，只有前序 job 全部
  成功后才会执行下一步；任何发布失败都不会部署不完整的新索引。
- workflow 使用固定 concurrency group `publish-and-deploy` 与
  `queue: max`（不设置 `cancel-in-progress: true`）。GitHub Actions 的
  `queue: max` 允许同一 group 的后续运行排队，最多保留 100 个 pending；
  新 push 不会替换旧 pending 运行。实际执行顺序不能保证严格按 push
  派发顺序，因此每个运行都必须在执行时查询真实 Release 状态并幂等
  收敛（已发布的跳过，未发布的验证后发布），不能依赖运行先后顺序。
  支持 `workflow_dispatch` 幂等恢复。
- workflow **不会向 main 回写任何内容**（方案 A：发布状态由 GitHub
  Release 推导，`generate_index` 读取已发布 tag），因此不会与 main
  分支保护冲突，也不会形成 push 循环。
- workflow 中的仓库名从 Actions Variables 读取，未设置时回退到生产
  默认值：`REGISTRY_OWNER=qingchenyouforcc`、
  `REGISTRY_REPO=NeurolingsCE-Mascots`、`VALIDATOR_REPO=qingchenyouforcc/NeurolingsCE`；
  staging 仓库必须显式设置这三个 Variables。
- PR workflow（`pr-validation.yml`）只产生 `registry-checks`，**不下载
  Draft asset**；Draft asset 的下载、SHA-256 与 CLI 再验证由投稿服务
  用 Publisher App installation token 完成，并以同名
  `package-validation` Check Run 报告（绑定 PR head SHA）。
- `publish_releases` 在发布前**再次**下载并验证每个 Draft asset
  （大小、SHA-256、真实 `NeurolingsCE-cli`），全部通过才
  PATCH `draft=false`；失败则保持 draft、不生成索引、不部署 Pages、
  不回写 main。
- main 分支保护使用 `checks` 对象按 `context` + `app_id` 固定检查来源：
  `registry-checks` → GitHub Actions App；`package-validation` →
  Publisher App。
- 源 manifest 不保存可推导的 `status`；`index-v1.json` 条目写入派生值
  `status: published`。
- 所有权：`owner.userId` 与 `maintainerUserIds`（GitHub numeric user ID）
  是权限依据；login 仅用于展示，改名不丢失权限。
- 第三方 Action 全部固定到完整 commit SHA；每个 workflow 显式声明最小
  permissions；除自动生成的 `GITHUB_TOKEN` 外不读取配置的 Secrets。
- 投稿服务运行 validator 时使用清理后的最小环境（不继承
  `GITHUB_*TOKEN*`、`AUTHORIZATION`、`COOKIE`、
  `SUBMISSION_SESSION_SECRET`、Publisher 私钥路径等），随机临时目录、
  超时/输出/资源限制、fail closed。
- staging 端到端流程见 `STAGING_E2E.md`。
