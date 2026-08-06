# NeurolingsCE-Mascots

NeurolingsCE 桌宠的公共注册表、投稿审核与分发仓库。

- `.mascot` 二进制包保存在 GitHub Releases（draft → 合并后发布）。
- GitHub Pages 发布静态 `generated/index-v1.json`，客户端只读取静态索引。
- 投稿通过 Pull Request 审核，合并后由 GitHub Actions 发布并重新生成索引。
- 仓库内容与审核记录保存在 `mascots/`，每个 mascot 一个目录。

## 目录

```text
mascots/<mascot-id>/manifest.json   # 注册表条目
schemas/                            # manifest/index JSON Schema
generated/index-v1.json             # Actions 生成的静态索引
tools/                              # 注册表校验与索引生成（stdlib-only Python）
submission-service/                 # 投稿服务（stdlib-only Python）
docs/                               # 投稿、审核、部署、安全文档
.github/workflows/                  # PR 校验、发布、Pages、清理
```

## 快速开始

```powershell
python tools/validate_registry.py .
python tools/generate_index.py .
python -m unittest discover -s tools/tests -p "test_*.py"
python -m unittest discover -s submission-service/tests -p "test_*.py"
```

维护者首次配置见 [docs/GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md) 与
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 许可

仓库代码与工具以 MIT 许可发布；`mascots/` 中的清单数据由各投稿作者提供，
其版权与许可证以各自 manifest 的 `license` 字段及原始素材为准。
