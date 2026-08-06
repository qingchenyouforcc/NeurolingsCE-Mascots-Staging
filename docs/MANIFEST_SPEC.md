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
| `authors` | 作者数组，每项含 `githubLogin` 与 `displayName` |
| `maintainers` | 有权发布新版本的 GitHub 登录名数组；更新 PR 的作者必须属于 maintainers |
| `license` | 紧凑 SPDX 标识（如 `MIT`、`CC-BY-4.0`） |
| `minimumNeurolingsCEVersion` | 所需最低客户端版本（SemVer） |
| `package` | `fileName`（`*.mascot`）、`url`、`size`、`sha256`（64 位小写 hex） |
| `createdAt` / `updatedAt` | ISO-8601 UTC |

## 可选字段

`upstream`、`isDerivative`、`icon`、`previews`、`tags`、`categories`、
`status`（`published`/`draft`）、`release`（Release/asset/tag 元数据）。

## 规则

- 同一 `id` 与 `version` 不允许重复发布；更新版本必须递增。
- 索引中不包含 `status: draft` 的条目。
- 下载 URL 由 manifest/索引提供，客户端不做拼接。
- 包 SHA-256 必须在合并前与实际 asset 校验一致。
