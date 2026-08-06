# Mascot Manifest 规范（v1）

每个 mascot 在 `mascots/<id>/manifest.json` 中维护一个 manifest。
完整约束见 `schemas/mascot-manifest-v1.schema.json` 与
`tools/registry_checks.py`（两者必须保持一致，修改时同步）。

## 必填字段

| 字段 | 说明 |
| --- | --- |
| `schemaVersion` | 固定 `"1"` |
| `id` | `^[a-z0-9]+(-[a-z0-9]+)*$`，≤64 字符；等于目录名；首版合并后不可被他人占用 |
| `name` | 展示名，≤120 字符 |
| `version` | SemVer（允许预发布/构建后缀） |
| `summary` | ≤300 字符简介 |
| `description` | 详细描述，≤20000 字符 |
| `authors` | 作者数组，`authors[0]` 必须含 `githubLogin`、`githubUserId` 与 `displayName`；`githubUserId` 为 GitHub numeric user ID |
| `maintainers` | 展示用 GitHub 登录名数组；`maintainers[i]` 与 `maintainerUserIds[i]` 按索引一一对应；权限依据是 `maintainerUserIds` |
| `maintainerUserIds` | **权限依据**：有权发布新版本的 GitHub numeric user ID 数组（唯一，且长度与顺序必须与 `maintainers` 严格一致） |
| `owner` | `{ "userId": <numeric>, "login": <login> }`；首版提交者即 owner，登录名改名不影响 owner 身份 |
| `license` | 紧凑 SPDX 标识（如 `MIT`、`CC-BY-4.0`） |
| `minimumNeurolingsCEVersion` | 所需最低客户端版本（SemVer） |
| `package` | `fileName`（`*.mascot`）、`url`、`size`、`sha256`（64 位小写 hex） |
| `createdAt` / `updatedAt` | ISO-8601 UTC |

## 可选字段

`upstream`、`isDerivative`、`icon`、`previews`、`tags`、`categories`、
`release`（Release/asset/tag 元数据）、`submissionId`。

`status` 已弃用：发布状态由 GitHub Release 推导，**新 manifest 不得写入
`status`**；`index-v1.json` 中的条目写入派生值 `status: published`。

## 规则

- 同一 `id` 与 `version` 不允许重复发布；更新版本必须**严格高于**已发布版本。
- 已有 ID 的更新者必须是 `maintainerUserIds` 中的 numeric user ID；
  login 改名不丢失权限，login 相同但 numeric ID 不同不获得权限。
- maintainer 成员集合以 `maintainerUserIds` 为准；numeric ID 集合任何
  增删都必须经现有 maintainer 或管理员批准。
- 登录名只用于展示：同一 numeric ID 的 login 可随 GitHub 改名刷新，
  投稿服务会用 session 中的当前 login 同步更新 `owner.login`、
  `authors[*].githubLogin` 与对应索引的 `maintainers[*]`；提交者不得
  修改其他 numeric ID 对应的 login，不得修改 owner 的 numeric ID。
- 同一 numeric ID 在 `owner`、`authors`、`maintainers` 中的 login 必须
  完全一致，由 `tools/registry_checks.py` 强制校验。
- 索引只包含 Release 已发布（非 draft）的条目，并写入派生
  `status: published`。
- 下载 URL 由 manifest/索引提供，客户端不做拼接。
- 包 SHA-256 必须在合并前与实际 asset 校验一致。

## 迁移说明（旧格式 manifest）

旧格式 manifest 只有 `maintainers`（login 数组），没有
`maintainerUserIds`。投稿服务对这类 manifest 的**新版本投稿 fail
closed**（`legacy_manifest_no_user_ids`），不会猜测 numeric ID 与 login
的对应关系。迁移步骤：

1. 由现有 maintainer 或仓库管理员逐条核对每个 login 的 GitHub numeric
   user ID（`GET /user` 返回的 `id`）；
2. 在 manifest 中新增 `maintainerUserIds`，保持与 `maintainers` 相同
   长度与顺序；
3. 给 `owner` 增加 `userId`（首版提交者的 numeric ID），并确保
   `owner.login`、`authors[0].githubLogin/githubUserId` 与维护者数组
   对应一致；
4. 通过 `tools/validate_registry.py . --json` 后再合并。

迁移后的 manifest 与新版一致：numeric ID 是权限依据，login 仅用于
展示并可随 GitHub 改名刷新。
