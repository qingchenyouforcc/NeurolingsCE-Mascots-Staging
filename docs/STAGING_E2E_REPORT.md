# Staging E2E 报告（真实 GitHub）

> 本文件只记录真实调用结果，不包含 token、私钥、session secret 或
> 完整 Authorization/Cookie。签名下载 URL 不落盘、不入库。

## 1. 已创建的真实资源

- Staging 仓库：`qingchenyouforcc/NeurolingsCE-Mascots-Staging`
  （public，默认分支 `main`）。
- 仓库 Variables：`REGISTRY_OWNER=qingchenyouforcc`、
  `REGISTRY_REPO=NeurolingsCE-Mascots-Staging`、
  `VALIDATOR_REPO=qingchenyouforcc/NeurolingsCE`。
- Pages：已开启，`build_type=workflow`；API 返回的 html_url 为
  `http://blog.qingchenyou.asia/NeurolingsCE-Mascots-Staging/`
  （该账号配置了自定义域名；最终可达 URL 以实际部署为准）。
- main 分支保护：required status checks `registry-checks` +
  `package-validation`（strict）、required PR review ×1、
  `enforce_admins=false`（staging 单账号可完成合并，生产需收紧）、
  `allow_force_pushes=false`、`allow_deletions=false`。

## 2. 测试夹具

- mascot id / version：`staging-test` `0.1.0`（合成、无版权素材，
  已通过真实 `NeurolingsCE-cli --mascot validate`）。
- Draft Release id：`366355280`，tag `draft/staging-test-0.1.0`。
- Asset id：`504150838`，文件名 `staging-test.mascot`，大小 655 字节，
  SHA-256 `f8d04c3cf502762f23bbe9fee3d1ea740b44eb308fb376bb8263e9e23c71fb59`。
- PR：`qingchenyouforcc/NeurolingsCE-Mascots-Staging#1`
  （head `submission/staging-test-0.1.0`，changed files=1）。

## 3. Draft asset 三个 API（真实，带用户 token 直接调用）

> 用户 token 与 workflow 自动 `GITHUB_TOKEN` 不同；下表是 API 行为
> 预验证，决定性结果以第 4 节 workflow 为准。

| API | HTTP | token 类型 | 结果 |
| --- | --- | --- | --- |
| GET release by ID | 200 | 用户 token（repo scope） | `draft=true` 可见 |
| LIST release assets | 200 | 同上 | 返回 asset 列表 |
| GET release asset | 302 | 同上 | Location 指向 `release-assets.githubusercontent.com`（签名 URL） |
| 跟随 302（无 Authorization） | 200 | 无 | 下载成功，655 字节 |

- 302 最终 host：`release-assets.githubusercontent.com`；第二个 host
  未收到 Authorization（测试请求未携带）。
- 下载 SHA-256 与 manifest 一致。
- 匿名访问 Draft Release：`404`（不可见）。

## 4. PR validation workflow（决定性验证）

- 首次 run：ID `31123472522`，event `pull_request`，触发于
  `2026-08-06T17:33:27Z`；两个 job 排队 15 分钟后被 **cancelled**。
- 原因：GitHub Actions/Pages **major outage**（incident
  `qcvjkzcs7j74`，自 `2026-08-06T15:22:49Z` 起，期间报告
  “Workflow runs are failing or delayed in starting, and some queued
  jobs may time out”）。
- 已执行 `gh run rerun`；attempt 2 同样在故障期间排队 15 分钟后被
  cancelled；attempt 3 在恢复后真实执行：

| 项目 | 真实结果 |
| --- | --- |
| run ID | `31123472522`（attempt 3，URL：`.../actions/runs/31123472522`） |
| workflow event | `pull_request` |
| job permissions | `contents: read` + `pull-requests: read`（workflow 声明值） |
| `registry-checks` | **success** |
| `package-validation` | **failure** |
| `GET /repos/<owner>/<repo>/releases/<release_id>` | **HTTP 403**（自动 `GITHUB_TOKEN`） |
| `GET .../releases/<id>/assets` | 未执行到（release 读取失败即停止） |
| `GET .../releases/assets/<asset_id>` | 未执行到 |
| PR check 最终结论 | failure（package-validation） |

### 结论（决定性）

`pull_request` workflow 中 `contents: read` + `pull-requests: read`
的自动 `GITHUB_TOKEN` **不能读取同仓库 Draft Release**（403），因此
Draft asset 最小验证 **未通过**，按 Gate 停止完整 E2E。

### 失败 Gate 处理

- 未提升 PR workflow 到 `contents: write`；
- 未向 workflow 注入 Publisher App 私钥；
- 未提前发布未经审核的 Release；
- 未使用长期共享下载 token；
- 完整 E2E（merge/publish/Pages/client）未继续。

### 可选替代方案（需维护者批准，未实施）

1. **发布时验证（推荐，权限最小）**：PR validation 只做 schema、
   重复与 changed-files 检查；Draft asset 的下载、SHA-256 与 CLI
   验证移到 `publish-and-deploy.publish_releases`（该 job 本来就以
   `contents: write` 验证并发布，Draft 对外不可见）。边界：不新增
   任何 workflow 权限；验证失败则 release 保持 draft，index/Pages
   不部署。
2. **独立只读验证 App**：新建 Contents: Read-only + Pull requests:
   Read-only 的 GitHub App，仅安装到 staging 仓库；PR validation 用
   短时 installation token 读取 Draft asset。边界：私钥作为仓库
   secret 注入（仅该 job 使用），仍只 checkout base SHA、不执行 PR
   代码；需维护者创建 App 并批准 secret 注入。
3. **窄范围 `contents: write` PR job**：仅在 package-validation job
   提升为 `contents: write`（其余保持只读）。边界：最小提升但仍宽于
   只读；需维护者明确批准；保持 base SHA checkout 与 changed-files
   白名单。

## 5. 状态摘要

| 环节 | 状态 |
| --- | --- |
| Staging 仓库/分支保护/Pages 配置 | Passed（真实） |
| 三个 Draft API（用户 token 预验证） | Passed（真实） |
| PR validation（GITHUB_TOKEN） | Failed（release GET 403，Gate 停止） |
| Merge → Publish → Index → Pages | Not run（Gate 停止） |
| 客户端刷新/下载/安装 | Not run |
| Device Flow / 投稿服务 | Not run（需两个 GitHub App，UI 创建） |

## 6. 未包含的敏感信息

本文件不包含：GITHUB_TOKEN、用户 token、Authorization 值、Cookie、
Publisher 私钥、session secret、签名下载 URL。

---

## 追加：Check Run 架构轮（2026-08-07）

### 已完成的真实结果

- `pr-validation.yml` 已改为仅 `registry-checks`（不再访问 Draft
  asset）；探针 PR #2（`staging-registry-probe`）两次真实运行：
  run `31146164896` 与 `31146449700` 均为 **success**（只读
  GITHUB_TOKEN，`contents: read` + `pull-requests: read`）。
- 分支保护已按 `checks` 对象固定来源：
  `registry-checks` → app_id `15368`（GitHub Actions，真实查询）；
  `package-validation` 待 Publisher App 创建后固定其 app_id。
- cleanup 真实测试：
  - PR #1 关闭 → run `31145911065` success，删除 Draft Release
    `366355280`；随后用正确端点手动删除遗留分支（原代码端点错误，
    已修复为 `/git/refs/...`）；
  - PR #2 关闭（Release 不存在，GitHub 对 integration token 返回
    403）→ run `31146624958` success，删除 submission 分支（403 已按
    absent 安全处理）。
- Pages HTTPS：`https_enforced=true`，证书 approved；
  `https://blog.qingchenyou.asia/NeurolingsCE-Mascots-Staging/index-v1.json`
  返回 **200** 且为合法 JSON（`github.io` URL 301 重定向到同一地址）。
- `publish-and-deploy` 真实 workflow_dispatch：
  run `31146613045` **success**（publish_releases / generate_index /
  deploy_pages 三 job 均 success）；修复索引 `registry` 字段后再次
  部署 run `31148037327` **success**，HTTPS 索引内容：
  `{"generatedAt": "2026-08-07T04:40:17Z", "mascots": [],
  "registry": "qingchenyouforcc/NeurolingsCE-Mascots-Staging",
  "schemaVersion": 1}`。
- 投稿服务本地：75 个服务测试 + 79/80 个工具测试通过（含 Check Run
  创建/更新、幂等恢复、head SHA 绑定、环境隔离、输出脱敏等新增测试）。

### 仍未完成（依赖维护者）

- 两个 GitHub App 未创建（UI 操作）：Login App 与 Publisher App
  （需 Checks Read/Write）。
- Publisher App `package-validation` Check Run 未真实创建；
  分支保护未绑定 Publisher App ID。
- 投稿服务 production 模式未部署；Device Flow / 上传 / 服务内
  Draft asset 下载与 CLI 再验证未真实运行。
- 客户端刷新/下载/安装未执行（索引为空且无已发布 mascot）。

---

## 追加：App 创建前检查轮（2026-08-07）

### 真实检查结果

- 通过 `GET /apps/{slug}` 检查四个候选 slug
  （`neurolingsce-login-staging`、`neurolingsce-publisher-staging`、
  `neurolingsce-login`、`neurolingsce-mascot-publisher`）均 **404**；
- staging 仓库既有 check runs 全部来自 GitHub Actions（app_id
  `15368`），不存在 Publisher App 产生的 `package-validation`
  Check Run；
- 本机未发现 Publisher App 私钥 PEM（仅发现无关的
  `StarlightGUI_TemporaryKey.pfx`）。

结论：**两个 staging GitHub App 尚未创建**，本轮停在外部依赖点，
未伪造任何 App/Check Run 结果。

### 本地完成项

- tools/tests：80 OK；submission-service/tests：75 OK（1 skip）；
  `validate_registry` ok；`generate_index` ok；
- 主仓库 Release：CTest 2/2 通过；
- localhost production 模式真实自检：
  - 缺 `SUBMISSION_SESSION_SECRET` → 拒绝启动（exit 2，fail closed）；
  - 随机 secret + 真实 `NeurolingsCE-cli`（Release）→ 启动自检通过，
    `GET /healthz` → `{"ok": true}`；
  - 服务标记：`env=production`、`validator=required`、
    `github_configured=False`（Publisher App 配置未设置，仅提交时使用）。

### 待维护者完成（外部依赖）

1. UI 创建 `NeurolingsCE Login Staging`（权限全 None、启用 Device
   Flow，记录 Client ID）；
2. UI 创建 `NeurolingsCE Publisher Staging`（Metadata
   Read-only、Contents Read/Write、Pull requests Read/Write、
   Checks Read/Write），仅安装到
   `qingchenyouforcc/NeurolingsCE-Mascots-Staging`，下载私钥 PEM；
3. 将私钥放到本机安全路径（如
   `D:\CPP_project\NeurolingsCE-Mascots\.secrets\publisher-app-key.pem`，
   该路径已被 `.gitignore` 的 `*.pem` 覆盖），并把
   `GITHUB_PUBLISHER_APP_ID`、`GITHUB_PUBLISHER_INSTALLATION_ID`、
   `GITHUB_PUBLISHER_PRIVATE_KEY_PATH` 写入服务端环境（不提交）；
4. 部署 HTTPS 投稿服务（或授权 localhost production 验证继续）。

维护者完成后，下一轮即可从“最小 Check Run 探针”继续。

---

## 追加：真实 E2E 执行轮（2026-08-07）

### GitHub Apps（真实验证）

- Login App：存在；Device Flow **真实通过**（device code → user_code →
  浏览器授权 → polling → access token → `GET /user`）；权限为 None
  （按创建配置）；Client ID 已配置到测试脚本（未写入仓库）。
- Publisher App：存在，App ID `4513799`，Installation ID
  `151870713`（用 App JWT 自动发现）；installation token 权限实测：
  `checks: write`、`contents: write`、`metadata: read`、
  `pull_requests: write`；`GET /installation/repositories` 仅返回
  `qingchenyouforcc/NeurolingsCE-Mascots-Staging`。

### Check Run 探针（真实）

| PR | head SHA | Check Run ID | app.id | conclusion |
| --- | --- | --- | --- | --- |
| 3 | `1c65672a…` | `92779927906` | 4513799 | success |
| 3（SHA B） | `4d0facc9…` | `92780770673` | 4513799 | success |
| 4（正式投稿） | `0992e9e9…` | `92784208491` | 4513799 | success |

### 防冒充（真实）

- A：Publisher 在 PR #3 head 创建成功 check，rollup=SUCCESS（required
  checks 满足；仅 review 阻塞，单账号 staging）。
- B：SHA A→B 后，SHA B 上无 `package-validation`（旧 check 未跟随）；
  Publisher 为 SHA B 重新创建 success 后恢复。
- C：用用户 PAT 在 SHA C 创建同名 commit status `package-validation`
  = success；SHA C 的 check-runs 仍只有 registry-checks，无
  `package-validation` check-run → 不能满足绑定 app_id 4513799 的
  required check。

### Branch protection（真实确认）

`registry-checks → app_id 15368`；`package-validation → app_id
4513799`；strict=true；review ×1；force push/deletion=false；
`enforce_admins=false`（staging）。

### Device Flow / 投稿服务（真实）

- Device Flow：**Passed**（login `qingchenyouforcc`，numeric user ID
  `90140161`；未记录任何 token）。
- 服务：localhost production（HTTPS 未部署 → 服务 staging 仍标记
  Not deployed）；`/healthz` ok；validator 自检通过；Publisher
  installation token 签发成功；持久存储使用中；日志无敏感值。

### 正式投稿（真实）

- submission `a798811599c24c46876aeef3`；PR #4；
  Release `366547997`（draft=true）；Asset `504731806`
  （`staging-e2e-0.1.0.mascot`，664 字节，SHA-256
  `3429e7880265fec0aa231a342660ce1cbb785d5bf4dcd3d0192df7328bf5af99`，
  与本地文件一致）。
- `package-validation`（app 4513799）success；`registry-checks`
  （app 15368）success。
- Merge：PR #4 合并（mergeCommit `07b093d1…`），使用 **staging-only
  管理员 bypass**（单账号无法 self-review，`enforce_admins=false`），
  不作为生产 review 验证。

### 发布二次验证（真实，fail closed）

- run `31152334227`（push）：publish_releases 下载真实 draft asset、
  SHA-256 一致，随后真实 `NeurolingsCE-cli` 校验**失败**：
  公开仓库 `qingchenyouforcc/NeurolingsCE` main 构建的 CLI 不支持
  `--mascot validate`（`invalid_arguments`；本地工作树已实现但未推送）。
- 结果：Release `366547997` 保持 `draft=true`；索引与 Pages 未更新
  （仍为 04:40 空索引）。这是按设计的 fail closed——未验证 asset
  不能发布。

### 阻塞点（需维护者决策）

1. 将本地 `NeurolingsCE` 工作树中的 CLI validator（`--mascot
   validate`）提交并推送到 `qingchenyouforcc/NeurolingsCE` main；或
2. 将 staging 仓库变量 `VALIDATOR_REPO` 指向已含该命令的公开仓库/分支；
   然后 `workflow_dispatch` 重跑 publish-and-deploy（Release 仍为
   draft，重跑将完成验证并发布）。

---

## 追加：CLI validator 合入与真实发布完成轮（2026-08-07）

### 方案 1 已执行（最小提交）

- 在干净 worktree（基于 `e06904b`）仅提交 8 个文件、645 行：
  `MascotPackage.hpp/.cc`、`SecurityLimits.hpp`、
  `CommandLineParser.cc`、`CommandExecutor.cc`、`OutputFormatter.cc`、
  `InternalCli.hpp`、`AppCoreTests.cc`（validator 测试与 helper）；
  不含 CMakeLists、UI/runtime/store、配置文件与 build 产物。
- commit `5854171`（`feat(cli): add mascot package validation
  command`）已推送到 `qingchenyouforcc/NeurolingsCE` main。
- 干净构建验证：Release 构建成功；CTest 2/2 通过；
  `NeurolingsCE-cli --json --mascot validate`：
  valid 包 exit 0（`ok:true`），invalid 包（缺 actions.xml）exit 1
  （`ok:false` + 错误列表）。

### 真实发布（workflow_dispatch 重跑）

- run `31153742626` **success**：publish_releases /
  generate_index / deploy_pages 三 job 均 success。
- 日志确认：`Published staging-e2e 0.1.0 (release 366547997)`，
  汇总 `{"alreadyPublished": 0, "published": 1,
  "skippedWithoutReleaseMetadata": 0}`。
- Release `366547997`：`draft=false`，`published_at=
  2026-08-07T06:24:53Z`。
- HTTPS 索引 `https://blog.qingchenyou.asia/NeurolingsCE-Mascots-Staging/
  index-v1.json`（200）包含：
  `id=staging-e2e`、`version=0.1.0`、`status=published`、
  `sha256=3429e7880265fec0aa231a342660ce1cbb785d5bf4dcd3d0192df7328bf5af99`、
  `size=664`、下载 URL 指向已发布 Release asset、maintainers
  `qingchenyouforcc`（manifest 中 numeric owner/maintainer ID 均为
  `90140161`，由 registry-checks 验证；索引按 schema 仅含 login）。
- 此前的失败 run `31152334227` 保留为 fail-closed 证据（validator
  缺失时拒绝发布）。

### Cleanup 复测（真实）

- 探针 PR #3 关闭 → cleanup run `31154131230` **success**；
  `submission/checkrun-probe-0.1.0` 分支已删除（404）；已发布
  `staging-e2e` Release 与索引不受影响。

### 剩余项（未在本轮执行）

- 客户端 Store 下载/安装/重启持久化（需 staging 配置的 Release 客户端）；
- `staging-e2e 0.1.1` 更新投稿与 `0.1.0` 重复版本拒绝；
- `staging-cleanup-test 0.1.0` 独立 cleanup 投稿；
- 投稿服务 HTTPS 公网部署（当前仅 localhost production）。
