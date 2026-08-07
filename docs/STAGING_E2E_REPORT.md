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
  deploy_pages 三 job 均 success）；索引已部署到 Pages。
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
