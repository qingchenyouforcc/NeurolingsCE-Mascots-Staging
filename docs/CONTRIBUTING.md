# 投稿指南

## 流程

1. 本地用客户端或 `NeurolingsCE-cli --mascot validate <file>.mascot --json`
   验证 `.mascot` 包。
2. 客户端（或手动）向投稿服务提交包与元数据，服务端创建 draft release
   并提交 PR。
3. 维护者审核 PR：manifest 字段、包内容、许可证与发布权。
4. 合并到受保护 `main` 分支后，发布 workflow 把 draft release 转为正式
   release、重新校验 SHA-256、生成索引并部署 Pages。

## 手动投稿（临时方式）

1. 新建分支，目录名必须等于 manifest 的 `id`。
2. 运行 `python tools/validate_registry.py .` 与
   `python tools/generate_index.py .`。
3. 上传 `.mascot` 到 GitHub Release asset，并把最终 URL/大小/SHA-256
   填入 `package`。
4. 提交 PR 并等待检查。

## 作者与维护者

- 首次发布：作者即默认维护者；后续新增维护者需管理员审核。
- 更新已有 mascot：PR 作者必须属于该 manifest 的 `maintainers`；
  否则 PR 会被检查 workflow 拒绝。
- 二次创作必须在 `upstream` 与 `isDerivative` 中如实声明，并在
  许可证/版权声明中保留原作者信息。

## 禁止内容

- 可执行/脚本载荷（`.exe`、`.dll`、`.sh`、`.js` 等）。
- 路径穿越、符号链接、嵌套压缩包、ZIP bomb。
- 未获授权发布的素材（角色版权、商标、他人作品）。
- 泄露 token、密钥或个人信息的包内容。
