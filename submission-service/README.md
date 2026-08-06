# 投稿服务

Python 3.11+（stdlib 优先）实现：

- `POST /v1/submissions`：multipart 上传（流式落盘、大小限制、幂等键）。
- `GET /v1/submissions/<id>`：状态查询（服务 token 或投稿者 token）。
- `DELETE /v1/submissions/<id>`：取消并清理 draft release/PR。
- `GET /healthz`：健康检查。

安全边界：

- 用户 token 只用于 `GET /user` 验证身份，不落库、不写日志、请求后释放。
- 官方仓库操作使用 GitHub App installation token（最小权限）。
- 日志统一脱敏（`redact.py`）。
- 包校验优先调用 `NeurolingsCE-cli --mascot validate`（`VALIDATOR_CLI`），
  未配置时使用嵌入式回退检查（`package_checks.py`，与客户端限制保持一致）。

部署与配置见 [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)。
