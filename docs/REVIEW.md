# 管理员审核与拒绝流程

## 审核清单

1. `pr-validation` workflow 全部通过（schema、重复、CLI 包验证、SHA-256）。
2. manifest 的 `authors`/`maintainers` 与 PR 作者一致；更新者属于 maintainers。
3. 许可证与来源声明合理；二次创作有 `upstream` 与 `isDerivative`。
4. 包内不包含可执行/脚本文件、路径穿越、嵌套压缩包。
5. draft release 的 asset 与 manifest 的 `package` 一致（大小 + SHA-256）。

## 合并

直接合并到 `main`。发布 workflow 会自动：

- 把对应 draft release 转正式 release；
- 再次下载并校验 SHA-256；
- 生成 `generated/index-v1.json`；
- 部署 GitHub Pages。

## 拒绝/关闭

关闭 PR 时 `cleanup-submissions.yml`（或服务端 `DELETE /v1/submissions/<id>`）
会删除对应 draft release。若已误合并，按 `RECOVERY.md` 回滚。

## 争议

涉及版权或恶意内容时：关闭 PR → 删除 draft release → 通知投稿者 →
必要时向平台举报并在 `SECURITY.md` 登记。
