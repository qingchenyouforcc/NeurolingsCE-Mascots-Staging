# 故障恢复与回滚

## 索引回滚

`generated/index-v1.json` 由 Actions 重新生成；回滚 = revert 对应 manifest
提交并重新运行 `publish-release` workflow。客户端在索引损坏时会保留旧缓存
并继续展示上次成功的索引。

## 误发布

- 将 GitHub Release 置为 draft 或删除，asset 会一并移除；
- revert manifest 提交；
- 重新生成索引并部署。

## 投稿服务故障

- 状态文件在 `SUBMISSION_STORAGE_DIR`，可直接查看/恢复；
- 服务无状态 GitHub 侧对象（draft release/分支/PR），重启幂等；
- 未完成的 draft release 可由 `DELETE /v1/submissions/<id>` 清理。

## 客户端缓存

删除 `AppLocalDataLocation/mascot-store-cache` 可强制刷新索引与已下载包。
