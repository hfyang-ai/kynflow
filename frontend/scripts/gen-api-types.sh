#!/usr/bin/env bash
# 从 FastAPI 的 OpenAPI schema 生成前端类型，避免前后端类型靠手抄导致 drift。
#
# 用法：npm run gen:api-types
# 依赖：backend/.venv（可用 `cd backend && uv sync` 准备）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/../backend"
PYTHON="$BACKEND/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "未找到后端虚拟环境：$PYTHON" >&2
  echo "请先执行：cd backend && uv sync" >&2
  exit 1
fi

TMP_SCHEMA="$(mktemp -t kynflow-openapi.XXXXXX.json)"
trap 'rm -f "$TMP_SCHEMA"' EXIT

echo "导出 OpenAPI schema..."
(
  cd "$BACKEND"
  # 只依赖 import app.main（不需要数据库连接）。
  "$PYTHON" -c "import json, sys; from app.main import app; sys.stdout.write(json.dumps(app.openapi()))"
) > "$TMP_SCHEMA"

echo "生成 TypeScript 类型..."
cd "$ROOT"
npx openapi-typescript "$TMP_SCHEMA" -o lib/api-schema.d.ts

echo "完成：frontend/lib/api-schema.d.ts"
