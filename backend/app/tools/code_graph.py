"""code.* 工具：基于 tree-sitter 符号表（code_symbols）的符号级查询。

只做「查符号」类工具，不做「追调用链」类工具。可靠性最高的数据是 code_symbols
的 path/name/kind/start_line/end_line——直接来自 tree-sitter 精确产出，无推断。

- code.read_symbol(path, name)：返回单个符号的完整函数体（按 start/end 行号切片）。
- code.outline(path)：返回文件的符号地图（name/kind/start/end）。
- code.find_symbol(name)：跨文件按符号名精确查找，返回列表（不覆盖同名）。
- code.pr_changed_symbols(pr_number)：PR 变更文件内的符号清单（touched-file 近似）。

read_symbol 返回 ``read_range``，供 ChatAgent 记录 read_coverage，避免后续
read_file 重复读取同一区间。
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import selectinload

from app.db.models import CodeSymbol, PullRequest
from app.services.code_analysis import _git_text
from app.services.code_graph import query_pr_symbols
from app.tools.context import ToolContext
from app.tools.workspace import (
    _read_workspace_text,
    _workspace_path,
    _workspace_relative,
    _workspace_root,
)

# 单个符号体最多返回的行数，超出截断并提示续读，避免超大符号抵消「省 token」收益。
SYMBOL_MAX_LINES = 300


class ReadSymbolParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1, description="相对工作区的文件路径")
    name: str = Field(min_length=1, description="符号名（函数/类/方法等）")
    kind: str | None = Field(default=None, description="符号类型，用于同名符号消歧")
    pr_number: int | None = Field(
        default=None, description="指定 PR 快照，默认主分支快照"
    )


class OutlineParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1, description="相对工作区的文件路径")
    pr_number: int | None = Field(
        default=None, description="指定 PR 快照，默认主分支快照"
    )


class FindSymbolParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, description="符号名")
    kind: str | None = Field(default=None, description="符号类型过滤")
    match: Literal["exact", "prefix", "contains"] = Field(
        default="exact",
        description="匹配方式：exact 精确匹配（默认）/ prefix 前缀匹配 / contains 子串匹配。prefix 与 contains 大小写敏感。",
    )
    limit: int = Field(default=20, ge=1, description="最多返回条数")
    pr_number: int | None = Field(
        default=None, description="指定 PR 快照，默认主分支快照"
    )


class PrChangedSymbolsParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    pr_number: int = Field(ge=1, description="PR 编号")
    limit: int = Field(default=50, ge=1, description="最多返回符号条数")


def _no_repo() -> dict[str, Any]:
    return {
        "route": "code_symbol",
        "tool_calls": [],
        "answer": "当前会话未关联仓库，无法查询代码符号。",
        "citations": [],
    }


def _symbol_rows(
    ctx: ToolContext,
    *,
    path: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    pr_number: int | None = None,
    limit: int = 100,
    name_match: Literal["exact", "prefix", "contains"] = "exact",
) -> list[CodeSymbol]:
    query = ctx.db.query(CodeSymbol).filter(CodeSymbol.repo_id == ctx.repo.id)
    if pr_number is not None:
        query = query.filter(CodeSymbol.pr_number == pr_number)
    else:
        query = query.filter(CodeSymbol.pr_id.is_(None))
    if path:
        query = query.filter(CodeSymbol.path == path)
    if name:
        # prefix/contains 走 startswith/contains（自动转义 %/_），exact 走 ==。
        if name_match == "prefix":
            query = query.filter(CodeSymbol.name.startswith(name))
        elif name_match == "contains":
            query = query.filter(CodeSymbol.name.contains(name))
        else:
            query = query.filter(CodeSymbol.name == name)
    if kind:
        query = query.filter(CodeSymbol.kind == kind)
    return (
        query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc())
        .limit(limit)
        .all()
    )


def _symbol_brief(symbol: CodeSymbol) -> dict[str, Any]:
    return {
        "path": symbol.path,
        "name": symbol.name,
        "kind": symbol.kind,
        "start": symbol.start_line,
        "end": symbol.end_line,
    }


def _numbered_lines(lines: list[str], start: int, end: int) -> str:
    return "\n".join(
        f"{line_number}: {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    )


async def read_symbol_tool(
    ctx: ToolContext, input_data: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    if ctx.repo is None:
        return _no_repo()
    params = ReadSymbolParams.model_validate(input_data)
    workspace_root = _workspace_root(ctx.repo)
    file_path = _workspace_path(workspace_root, params.path)
    relative = _workspace_relative(file_path, workspace_root)

    rows = _symbol_rows(
        ctx, path=relative, name=params.name, pr_number=params.pr_number, limit=50
    )
    if params.kind:
        rows = [row for row in rows if row.kind == params.kind]

    if not rows:
        answer = (
            f"未在 {relative} 找到名为「{params.name}」的符号定义"
            f"{f'（kind={params.kind}）' if params.kind else ''}。"
            "请用 code.outline 查看该文件已索引的符号，或改用 read_file 直接读文件。"
        )
        return {
            "route": "code_read_symbol",
            "tool_calls": [
                {
                    "tool_name": "code.read_symbol",
                    "arguments": params.model_dump(),
                    "status": "success",
                }
            ],
            "answer": answer,
            "citations": [
                {"type": "workspace_file", "id": relative, "title": relative}
            ],
        }

    if len(rows) > 1:
        briefs = [_symbol_brief(row) for row in rows]
        answer = (
            f"{relative} 里有 {len(rows)} 个名为「{params.name}」的符号，"
            "为避免读错符号体，请用 code.read_symbol 补充 kind 参数消歧：\n"
            + json.dumps(briefs, ensure_ascii=False)
        )
        return {
            "route": "code_read_symbol",
            "tool_calls": [
                {
                    "tool_name": "code.read_symbol",
                    "arguments": params.model_dump(),
                    "status": "success",
                }
            ],
            "answer": answer,
            "citations": [
                {"type": "workspace_file", "id": relative, "title": relative}
            ],
        }

    symbol = rows[0]
    if not symbol.start_line or not symbol.end_line:
        answer = (
            f"符号 {symbol.name}（{symbol.kind}）缺少行号信息，"
            "无法定位函数体。请用 code.outline 查看文件结构，或用 read_file 直接读。"
        )
        return {
            "route": "code_read_symbol",
            "tool_calls": [
                {
                    "tool_name": "code.read_symbol",
                    "arguments": params.model_dump(),
                    "status": "success",
                }
            ],
            "answer": answer,
            "citations": [
                {"type": "workspace_file", "id": relative, "title": relative}
            ],
        }

    text = _read_workspace_text(file_path)
    lines = text.splitlines()
    total = len(lines)
    start = max(symbol.start_line, 1)
    end = min(symbol.end_line, total)
    truncated = False
    if end - start + 1 > SYMBOL_MAX_LINES:
        end = start + SYMBOL_MAX_LINES - 1
        truncated = True

    # 新鲜度兜底：符号行号来自同步快照，实时 checkout 可能已有新提交。
    head = _git_text(["rev-parse", "HEAD"], workspace_root)
    stale = bool(symbol.commit_sha and head and symbol.commit_sha[:7] != head[:7])

    header = f"{relative}:{start}-{end}（符号 {symbol.name}[{symbol.kind}]）"
    if stale:
        header += (
            f"\n⚠️ 符号行号来自快照 commit {symbol.commit_sha[:7]}，"
            f"当前 checkout HEAD 是 {head[:7]}，行号可能已过期，请优先用 read_file 核对。"
        )
    if truncated:
        header += (
            f"\n（符号体超过 {SYMBOL_MAX_LINES} 行，已截断到 {end} 行；"
            f"续读请用 read_file start_line={end + 1}）"
        )
    answer = header + "\n" + _numbered_lines(lines, start, end)
    return {
        "route": "code_read_symbol",
        "tool_calls": [
            {
                "tool_name": "code.read_symbol",
                "arguments": params.model_dump(),
                "status": "success",
            }
        ],
        "answer": answer,
        "citations": [{"type": "workspace_file", "id": relative, "title": relative}],
        "read_range": {
            "path": relative,
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "truncated": end < total,
            "next_start_line": end + 1 if end < total else None,
        },
    }


async def outline_tool(
    ctx: ToolContext, input_data: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    if ctx.repo is None:
        return _no_repo()
    params = OutlineParams.model_validate(input_data)
    workspace_root = _workspace_root(ctx.repo)
    file_path = _workspace_path(workspace_root, params.path)
    relative = _workspace_relative(file_path, workspace_root)

    rows = _symbol_rows(
        ctx, path=relative, pr_number=params.pr_number, limit=500
    )
    if not rows:
        answer = (
            f"{relative} 没有已索引的符号。请确认仓库已同步代码图，"
            "或改用 read_file 直接读文件。"
        )
    else:
        answer = json.dumps(
            {
                "path": relative,
                "symbols": [_symbol_brief(row) for row in rows],
            },
            ensure_ascii=False,
        )
    return {
        "route": "code_outline",
        "tool_calls": [
            {
                "tool_name": "code.outline",
                "arguments": params.model_dump(),
                "status": "success",
            }
        ],
        "answer": answer,
        "citations": [{"type": "workspace_file", "id": relative, "title": relative}],
    }


async def find_symbol_tool(
    ctx: ToolContext, input_data: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    if ctx.repo is None:
        return _no_repo()
    params = FindSymbolParams.model_validate(input_data)
    rows = _symbol_rows(
        ctx,
        name=params.name,
        kind=params.kind,
        pr_number=params.pr_number,
        limit=min(params.limit, 100),
        name_match=params.match,
    )
    if not rows:
        match_desc = {
            "exact": f"精确名为「{params.name}」",
            "prefix": f"以「{params.name}」开头",
            "contains": f"包含「{params.name}」",
        }[params.match]
        answer = (
            f"没有找到{match_desc}的符号定义"
            f"{f'（kind={params.kind}）' if params.kind else ''}。"
            "符号表只收录 tree-sitter 解析出的定义，不含注释、文档或调用点。"
            "请换一个符号名或放宽匹配方式（match=contains），"
            "或改用 workspace.search_code 做文本检索。"
        )
    else:
        answer = json.dumps([_symbol_brief(row) for row in rows], ensure_ascii=False)
    return {
        "route": "code_find_symbol",
        "tool_calls": [
            {
                "tool_name": "code.find_symbol",
                "arguments": params.model_dump(),
                "status": "success",
            }
        ],
        "answer": answer,
        "citations": [
            {"type": "workspace_file", "id": row.path, "title": row.path}
            for row in rows
        ],
    }


async def pr_changed_symbols_tool(
    ctx: ToolContext, input_data: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    if ctx.repo is None:
        return _no_repo()
    params = PrChangedSymbolsParams.model_validate(input_data)
    pr = (
        ctx.db.query(PullRequest)
        .options(selectinload(PullRequest.files))
        .filter(
            PullRequest.repo_id == ctx.repo.id,
            PullRequest.number == params.pr_number,
        )
        .one_or_none()
    )
    if pr is None:
        return {
            "route": "code_pr_changed_symbols",
            "tool_calls": [
                {
                    "tool_name": "code.pr_changed_symbols",
                    "arguments": params.model_dump(),
                    "status": "success",
                }
            ],
            "answer": f"仓库 {ctx.repo.full_name} 没有编号为 #{params.pr_number} 的 PR。",
            "citations": [],
        }

    rows = query_pr_symbols(ctx.db, pr, limit=min(params.limit, 200))
    if not rows:
        answer = (
            f"PR #{pr.number} 的变更文件里没有已索引的符号"
            "（可能是 PR 快照尚未生成，请先在 PR 详情页准备快照）。"
        )
        citations = []
    else:
        answer = (
            f"PR #{pr.number} 涉及 {len(rows)} 个符号"
            "（touched-file 近似：变更文件内的所有符号，非精确增删改集）：\n"
            + json.dumps([_symbol_brief(row) for row in rows], ensure_ascii=False)
            + "\n\n要读某个符号体，请用 code.read_symbol(path, name)。"
        )
        citations = [
            {"type": "pull_request", "id": str(pr.id), "title": pr.title}
        ]
    return {
        "route": "code_pr_changed_symbols",
        "tool_calls": [
            {
                "tool_name": "code.pr_changed_symbols",
                "arguments": params.model_dump(),
                "status": "success",
            }
        ],
        "answer": answer,
        "citations": citations,
    }
