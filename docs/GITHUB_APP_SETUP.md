# GitHub App 创建与安装

## 创建 App

1. 打开 https://github.com/settings/apps/new。
2. 名称示例：`NeurolingsCE-Mascots-Submission`；Homepage 填仓库地址。
3. Webhook：不需要，可关闭。
4. 权限（最小化）：
   - Repository contents: **Read & write**
   - Releases: **Write**
   - Metadata: **Read**
5. 安装范围：仅 `qingchenyouforcc/NeurolingsCE-Mascots`。
6. 记录：
   - App ID
   - Client ID（编译进客户端，公开）
   - Installation ID（从安装 URL 中获取）
   - 私钥 PEM（仅服务端）

## 客户端编译期配置

```text
NEUROLINGSCE_GITHUB_APP_CLIENT_ID=<公开 Client ID>
NEUROLINGSCE_GITHUB_APP_ID=<公开 App ID>
NEUROLINGSCE_SUBMISSION_SERVICE_URL=https://submissions.example.com
NEUROLINGSCE_MASCOT_INDEX_URL=https://<owner>.github.io/NeurolingsCE-Mascots/index-v1.json
```

这些值会生成到 `include/shijima-qt/MascotStoreConfig.hpp`（占位符）。
任何 secret 都不允许出现在客户端配置中。

## 服务端 Secret

| Secret | 说明 |
| --- | --- |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_APP_INSTALLATION_ID` | 安装 ID |
| `GITHUB_APP_PRIVATE_KEY` | PEM 私钥内容 |
| `SUBMISSION_SERVICE_TOKEN` | 可选服务间认证令牌 |

私钥轮换与撤销见 `TOKEN_ROTATION.md`。
