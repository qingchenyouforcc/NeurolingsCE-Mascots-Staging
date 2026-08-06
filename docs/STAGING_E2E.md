# Staging 端到端验证（真实 GitHub）

公开投稿前必须完整跑通一次真实 staging E2E；**mock 测试不能代替**。
未跑通前“公开投稿”保持 **Blocked**。本清单只接受真实 GitHub/真实
投稿服务的调用结果。

## 原则

- 私钥与 token 不写入源码、不粘贴到聊天、不落日志；staging 使用独立
  测试仓库、测试 App、测试账号与测试 mascot，与生产完全隔离。
- PR validation 阶段若只读 `GITHUB_TOKEN` 无法读取 Draft asset：
  不提升 workflow 权限、不提前公开 Draft Release，**立即停止 E2E**，
  记录三个 API 的真实响应，输出可选替代方案及其权限边界，等待维护者
  决策。
- 每一步记录真实结果（workflow event、HTTP 状态、最终 host、
  SHA-256），不得把 mock 结果填入“真实结果”。

## 0. 准备测试仓库

1. 创建独立测试仓库（不与生产 `NeurolingsCE-Mascots` 混用）：
   ```bash
   gh repo create qingchenyouforcc/NeurolingsCE-Mascots-Staging \
     --public --source=<local-mascots-repo> --remote=staging --push
   gh repo edit qingchenyouforcc/NeurolingsCE-Mascots-Staging \
     --default-branch main --enable-issues=false \
     --enable-wiki=false --allow-forking=false
   ```
2. 推入与生产一致的 workflow、schema、工具与投稿服务代码（先本地测试）。
3. 在仓库 Settings → Secrets and variables → Actions → Variables 设置
   （未设置时 workflow 自动回退到生产默认值）：
   ```text
   REGISTRY_OWNER=qingchenyouforcc
   REGISTRY_REPO=NeurolingsCE-Mascots-Staging
   VALIDATOR_REPO=qingchenyouforcc/NeurolingsCE
   ```
   或用命令：
   ```bash
   gh variable set REGISTRY_OWNER -R qingchenyouforcc/NeurolingsCE-Mascots-Staging --body qingchenyouforcc
   gh variable set REGISTRY_REPO -R qingchenyouforcc/NeurolingsCE-Mascots-Staging --body NeurolingsCE-Mascots-Staging
   gh variable set VALIDATOR_REPO -R qingchenyouforcc/NeurolingsCE-Mascots-Staging --body qingchenyouforcc/NeurolingsCE
   ```
4. 记录 `owner`/`repo`，作为投稿服务 `GITHUB_OWNER`/`GITHUB_REPO`。

## 1. Login App（无仓库权限）

- GitHub → Settings → Developer settings → GitHub Apps → New GitHub App。
- Permissions：**全部 None**；勾选 **Enable Device Flow**。
- 记录 `Client ID`（可公开），配置到桌面客户端编译期
  `NEUROLINGSCE_GITHUB_LOGIN_CLIENT_ID`。

## 2. Publisher App（仅安装到测试仓库）

- New GitHub App；Repository permissions 最小化：
  Contents Read/Write、Metadata Read、Pull requests Read/Write。
- 仅安装到测试 Registry 仓库，不授予组织级或仓库外权限。
- 记录 `App ID`、`Installation ID`，私钥 PEM 只放在服务端 Secret
  （`GITHUB_PUBLISHER_PRIVATE_KEY_PATH`），不入库。

## 3. 部署投稿服务 staging

```bash
export SUBMISSION_ENV=production
export SUBMISSION_SESSION_SECRET="$(openssl rand -hex 32)"
export VALIDATOR_CLI=/path/to/NeurolingsCE-cli
export GITHUB_PUBLISHER_APP_ID=<app id>
export GITHUB_PUBLISHER_INSTALLATION_ID=<installation id>
export GITHUB_PUBLISHER_PRIVATE_KEY_PATH=/run/secrets/publisher-app-key.pem
export GITHUB_OWNER=<staging owner>
export GITHUB_REPO=<staging repo>
python submission-service/app.py
```

- `SUBMISSION_SESSION_SECRET` 必须固定并 ≥32 字节（hex/base64），重启
  后不变；生产模式缺 secret/validator 必须拒绝启动（自检）。
- `VALIDATOR_CLI` 用真实 CLI；服务启动自检通过后 `/healthz` 返回 ok。

## 4. 开启 Pages 与 main 分支保护

- Settings → Pages → Source：GitHub Actions（部署来自
  `publish-and-deploy.yml` 的 artifact）。
- Settings → Branches → main：开启保护，要求 PR 与 required status
  checks（含 `pr-validation`），禁止直接 push。

```bash
gh api -X POST repos/qingchenyouforcc/NeurolingsCE-Mascots-Staging/pages \
  -f build_type=workflow
gh api -X PUT repos/qingchenyouforcc/NeurolingsCE-Mascots-Staging/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["registry-checks", "package-validation"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "allow_force_pushes": false
}
JSON
```

> Pages 在 Free 账号下要求仓库为 public；如必须私有，需 Pro 及以上套餐。

## 5. 真实 Device Flow

1. 桌面客户端发起 Device Flow（测试账号）→ 浏览器授权。
2. 客户端拿 user token 调 `POST /v1/auth/github` → session token。
3. 记录：device flow 状态、auth HTTP 状态码、session 有效期。

## 6. 上传测试 mascot

```bash
python tools/staging_e2e.py \
  --package /path/valid.mascot \
  --metadata /path/metadata.json \
  --owner <staging owner> --repo <staging repo> \
  --report staging-report.json
```

环境变量：`STAGING_SERVICE_URL`、`STAGING_GITHUB_TOKEN`（仅测试账号，
仅 staging）。记录 submission id、Draft Release id、asset id、PR 号。

## 7. Draft Release / asset / 分支 / PR

服务端自动完成：创建 draft release → 上传 asset → 建分支 → 写
manifest → 开 PR。逐项核对并记录：

- `GET /repos/<owner>/<repo>/releases/<release_id>`：状态码、`draft: true`；
- `GET /repos/<owner>/<repo>/releases/<release_id>/assets`：状态码、
  分页（Link Header）与列表；
- `GET /repos/<owner>/<repo>/releases/assets/<asset_id>`：状态码、
  200 或 302；302 时最终 host；第二个 host 是否收到 `Authorization`
  （必须为否）；仍为 HTTPS；
- 下载内容 SHA-256 与 manifest `package.sha256` 一致。

## 8. PR validation（必须记录的真实结果）

等待 PR check。必须记录：

- workflow event（`pull_request`）与 Actions run id；
- job 的 `GITHUB_TOKEN` permissions（`contents: read` +
  `pull-requests: read`，不得更宽）；
- `GET /repos/<owner>/<repo>/releases/<release_id>`：HTTP 状态码、
  Draft Release 是否可见（`draft` 值；匿名访问必须不可见）；
- `GET /repos/<owner>/<repo>/releases/<release_id>/assets`：HTTP 状态码
  与分页；
- `GET /repos/<owner>/<repo>/releases/assets/<asset_id>`：HTTP 状态码
  （200 或 302）；
- 302 时最终 host（例如 `objects.githubusercontent.com`）与 token 是否
  被转发（必须为否）；
- 下载文件 SHA-256；
- PR check 最终状态（success/failure）与失败原因。

**Gate**：若只读 `GITHUB_TOKEN` 读取 Draft asset 返回 403/404，按
“原则”停止；替代方案（例如用 Publisher App 安装 token 的独立发布
workflow）必须说明其权限边界，未经维护者批准不得实施。

## 9. 合并 PR

分支保护 required checks 通过后合并；记录合并方式与 merge SHA。

## 10. 发布 Release → 生成索引 → 部署 Pages

- push main 触发 `publish-and-deploy.yml`；记录 event
  （`push`/`workflow_dispatch`）、run id、每个 job 的 permissions
  （`contents: write` / `contents: read` / `pages: write` +
  `id-token: write`）。
- `publish_releases`：验证 asset 后 PATCH draft → published，记录
  `GET release` 返回 `draft: false`。
- `generate_index`：读取已发布 tag 生成 `index-v1.json` 并上传 artifact。
- `deploy_pages`：部署成功后记录 Pages URL 与索引条目
  `status: published`。

## 11. 客户端刷新、下载、校验、安装

用真实客户端 build：

1. 刷新索引 → 出现测试 mascot；
2. 下载 package → SHA-256 校验通过 → 安装成功并显示；
3. 记录客户端版本、日志关键行；失败时记录完整错误。

## 12. 失败流程（至少 8 项，每项记录真实结果）

- 用户取消 Device Flow → `access_denied`；
- session token 过期（>10 分钟）→ `401 auth_invalid`；
- 同幂等键重复上传 → 返回原 submission；
- 非法 ID / 越界 changed files → PR validation 失败；
- asset SHA-256 与 manifest 不符 → fail closed；
- PR 关闭后 cleanup 只删验证过的 draft release；
- cleanup 重复执行 / 迟到 → 不误删已发布 release；
- GitHub API 429 → 尊重 `Retry-After`；
- publish workflow 重跑 → 幂等；
- Pages 部署失败 → 修复后 `workflow_dispatch` 恢复；
- 投稿服务中途重启 → 幂等键恢复未完成步骤。

## 自动化脚本

`tools/staging_e2e.py` 提供 `--dry-run`（无副作用）与真实执行两种模式；
真实模式要求 `STAGING_ENV=staging`、`STAGING_SERVICE_URL` 与
`STAGING_GITHUB_TOKEN`（仅测试账号）。脚本把三个 API 的真实响应、
重定向、SHA-256 与 PR check 写入 `--report` JSON。token 绝不写日志。

## 验收

- 正常流程每个环节有真实日志/API 证据；
- “必须记录的真实结果”逐项填写；
- 失败流程至少 8 项有记录；
- 未跑通前，公开投稿保持 **Blocked**。
