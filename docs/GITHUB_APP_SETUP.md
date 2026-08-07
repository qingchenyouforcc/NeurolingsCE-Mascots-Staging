# GitHub App 创建与安装

投稿闭环使用**两个独立 GitHub App**，权限与密钥边界完全分离：

## App A：NeurolingsCE Login（桌面客户端）

用途：

- 桌面客户端 Device Flow 登录；
- 只读取用户 `login`、numeric user ID、avatar（`GET /user`）；
- 客户端把 user access token 交给 `POST /v1/auth/github` 换取短期
  submission session token。

权限：**全部为 None**。

```text
Repository permissions: None
Organization permissions: None
Account/User permissions: None
```

说明：

- `GET /user` 不需要任何额外 App 权限；GitHub App Device Flow 不使用传统
  OAuth scope，设备码请求只发送 `client_id`，**不发送 `scope`**。
- 不需要安装到 `NeurolingsCE-Mascots` 仓库。
- 客户端只包含 Login App 的 Client ID（公开值），不包含 Login App 私钥，
  也绝不包含 Publisher App 的任何信息。
- 在 App 设置的 "Device authorization" 中启用 Device Flow，否则客户端
  登录会收到 `device_flow_disabled`。

记录：

- App ID（公开，可编译进客户端）
- Client ID（公开，编译进客户端）
- 无 Installation ID（不安装）
- 无私钥（客户端无需）

客户端编译期配置：

```text
NEUROLINGSCE_GITHUB_LOGIN_CLIENT_ID=<Login App Client ID>
NEUROLINGSCE_SUBMISSION_SERVICE_URL=https://submissions.example.com
NEUROLINGSCE_MASCOT_INDEX_URL=https://<owner>.github.io/NeurolingsCE-Mascots/index-v1.json
```

## App B：NeurolingsCE Mascot Publisher（投稿服务端）

用途（只运行在投稿服务端）：

- 创建 Draft Release；
- 上传 asset；
- 创建投稿分支、写入 manifest；
- 创建和关闭 PR；
- 清理投稿资源。

Repository permissions（最小化，唯一需要的权限）：

```text
Metadata: Read-only
Contents: Read and write
Pull requests: Read and write
Checks: Read and write
```

说明：

- 不需要独立的 `Releases: Write`（Release API 由 Contents 覆盖）；
- `Checks: Read and write` 的唯一用途：投稿服务在投稿分支的精确
  head SHA 上创建/更新 `package-validation` Check Run；不用于修改
  Actions workflow，也不用于客户端；
- 不申请 `Workflows: Write`、邮箱/组织/私有仓库等权限；
- Publisher App 的 Client ID **不用于桌面 Device Flow**，桌面客户端永远
  不能获得 Publisher App 的 user access token；
- 生产只安装到 `qingchenyouforcc/NeurolingsCE-Mascots`；staging 只安装到
  `qingchenyouforcc/NeurolingsCE-Mascots-Staging`；
- 私钥 PEM 只存在于投稿服务端，绝不进入客户端或仓库。

## GitHub App 创建方式

GitHub App **不能通过普通已认证 REST 请求直接创建**；维护者可以通过
GitHub UI 创建，也可以采用 GitHub App Manifest Flow，但 Manifest Flow
仍需要浏览器授权和一次性 code 交换。本仓库不要求为自动化实现
Manifest Flow，优先使用 UI 创建两个 staging App。

记录：

- App ID → `GITHUB_PUBLISHER_APP_ID`
- Installation ID → `GITHUB_PUBLISHER_INSTALLATION_ID`
- 私钥 PEM 路径 → `GITHUB_PUBLISHER_PRIVATE_KEY_PATH`

## 服务端 Secret（投稿服务）

| Secret | 说明 |
| --- | --- |
| `GITHUB_PUBLISHER_APP_ID` | Publisher App ID |
| `GITHUB_PUBLISHER_INSTALLATION_ID` | Publisher App 安装 ID |
| `GITHUB_PUBLISHER_PRIVATE_KEY_PATH` | Publisher App PEM 私钥文件路径 |
| `SUBMISSION_SESSION_SECRET` | **生产必填**：hex（≥64 字符）或 base64，解码后 ≥32 字节 |
| `SUBMISSION_SERVICE_TOKEN` | 可选服务间认证令牌（不作为安全边界） |

私钥轮换与撤销见 `TOKEN_ROTATION.md`。

## Staging 部署差异

- 测试仓库：`qingchenyouforcc/NeurolingsCE-Mascots-Staging`（独立资源，
  不混用生产数据）。
- Login App：建议名称 `NeurolingsCE Login Staging`，权限仍为全部 None，
  不安装到任何仓库。
- Publisher App：建议名称 `NeurolingsCE Mascot Publisher Staging`，
  Repository permissions 同上（Metadata: Read-only、Contents: Read and
  write、Pull requests: Read and write、Checks: Read and write），
  只安装到 staging 仓库。
- 仓库 Actions Variables（未设置时 workflow 回退到生产默认值）：
  `REGISTRY_OWNER`、`REGISTRY_REPO`、`VALIDATOR_REPO`。
- 客户端 staging 配置：

  ```text
  NEUROLINGSCE_MASCOT_INDEX_URL=https://blog.qingchenyou.asia/NeurolingsCE-Mascots-Staging/index-v1.json
  NEUROLINGSCE_SUBMISSION_SERVICE_URL=https://<staging 服务 HTTPS 地址>
  ```

- 所有 secret（Publisher 私钥、session secret）只存在于 staging 服务
  环境，不写入源码、仓库或聊天。

## 分支保护：检查来源固定

main 分支保护使用 branch protection API 的 `checks` 对象，按
`context` + `app_id` 固定检查来源，而不是只按字符串要求：

- `registry-checks`：来源固定为 GitHub Actions App（先查询实际
  `app.id` 再写入）；
- `package-validation`：来源固定为 Publisher App 的 App ID（创建 App
  后从 Check Run 响应或 `GET .../check-runs` 查询 `app.id`）。

同名但来源错误的 status/check 不能满足 required check。
