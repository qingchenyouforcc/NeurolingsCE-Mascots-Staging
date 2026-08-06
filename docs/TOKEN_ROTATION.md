# Token 与私钥轮换

## GitHub App 私钥

1. 在 GitHub App 设置中生成新私钥。
2. 更新服务端 Secret `GITHUB_APP_PRIVATE_KEY`。
3. 部署后验证 `/healthz` 与一次测试投稿。
4. 确认新私钥工作后删除旧私钥。

## 用户 access token

客户端 Device Flow 的 refresh token 过期后自动刷新；刷新失败回到未登录状态。
用户可在 GitHub 设置中撤销授权，客户端下次请求会收到 401 并登出。

## 泄露响应

- 私钥泄露：立即撤销私钥、重新生成、检查仓库近期写入记录。
- 用户 token 泄露：提示用户撤销授权；服务端不保存用户 token，无需清洗数据库。
