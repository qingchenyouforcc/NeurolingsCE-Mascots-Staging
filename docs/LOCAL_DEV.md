# 本地开发

## 前置

- Python 3.11+（仅标准库即可运行工具与测试）。
- 可选：`NeurolingsCE-cli`（本地包验证）。

## 命令

```powershell
python tools/validate_registry.py . --json
python tools/generate_index.py . --generated-at 2026-08-06T00:00:00Z
python -m unittest discover -s tools/tests -p "test_*.py"
python -m unittest discover -s submission-service/tests -p "test_*.py"
```

## 本地运行投稿服务（无 GitHub 凭据）

```powershell
$env:SUBMISSION_STORAGE_DIR = "$env:TEMP\neurolingsce-submission-dev"
python submission-service/app.py
```

没有 GitHub App 凭据时服务仍可启动并接收投稿，但创建 PR 的步骤会返回
`github_unconfigured` 结构化错误；可配合 `submission-service/tests`
中的模拟 GitHub 服务器进行端到端验证。
