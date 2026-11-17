# Database Schema

核心表：users、repositories、issues、pull_requests、pr_files、pr_review_comments、workflow_runs、analysis_results、documents、agent_runs、eval_runs、code_symbols、code_relations。

当前骨架模型位于 `backend/app/db/models.py`。

补充说明：

- `workflow_runs.jobs` 保存 GitHub Actions jobs 和 steps，用于定位失败 job。
- `documents.metadata` 保存标签、文件路径、状态和时间范围，用于 RAG 过滤。
- `eval_runs.result_json` 保存基础评测指标和 Markdown eval report。
- `code_symbols` 保存 tree-sitter 解析出的符号定义（path/name/kind/start_line/end_line/commit_sha），按仓库与 PR 快照隔离（`pr_id`/`pr_number`），`start_line`/`end_line` 直接来自 tree-sitter 精确产出。`code.*` 符号查询工具只依赖本表，不推断调用链。
- `code_relations` 保存符号间关系（`relation_type` 为 calls/imports）。`target_name` 是 module 字符串（imports 未解析成文件）、`target_symbol_id` 为裸名匹配结果（calls 大量为 None），可靠性低于 `code_symbols` 本身，目前仅用于分析展示，不做调用链推理。
