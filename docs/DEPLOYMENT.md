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
| `GITHUB_APP_ID` | — | GitHub App ID |
| `GITHUB_APP_INSTALLATION_ID` | — | 安装 ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | PEM 文件路径 |
| `GITHUB_OWNER` / `GITHUB_REPO` | `qingchenyouforcc` / `NeurolingsCE-Mascots` | 官方仓库 |
| `MAX_UPLOAD_BYTES` | `104857600`（100 MiB） | 包大小上限 |
| `RATE_LIMIT_SUBMISSIONS_PER_MINUTE` | `10` | 每用户/每 IP 速率限制 |
| `SUBMISSION_SERVICE_TOKEN` | — | 可选 Bearer 服务认证 |
| `VALIDATOR_CLI` | — | 可选：`NeurolingsCE-cli` 路径，优先调用公共验证器 |

## 启动

```bash
python -m pip install -r submission-service/requirements.txt
export GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/app-key.pem
python submission-service/app.py
```

生产建议：放在反向代理（HTTPS）之后，限制请求体大小，
并将 `SUBMISSION_STORAGE_DIR` 挂到持久卷。

## 健康检查

`GET /healthz` 返回 `{"ok":true}`，不依赖 GitHub。
