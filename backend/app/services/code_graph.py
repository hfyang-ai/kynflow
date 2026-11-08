"""代码符号图的共享查询层，供 API 路由与对话工具复用。

``code_symbols`` / ``code_relations`` 由 tree-sitter 同步产出（见
``app/services/code_analysis.py``）。本模块只暴露「查符号」类查询，不暴露调用链
查询：``code_relations`` 的 ``calls`` 边是裸名匹配（``source_symbol_id`` 归到文件
首个符号、``target_symbol_id`` 大量为 None），``imports`` 边的 ``target_name`` 是未
解析成文件的 module 字符串，接入会给模型喂噪声。
"""

from sqlalchemy.orm import Session

from app.db.models import CodeSymbol, PullRequest


def pr_changed_paths(pr: PullRequest) -> list[str]:
    """PR 涉及的文件路径（去重保序）。"""
    return list(dict.fromkeys(file.filename for file in pr.files if file.filename))


def query_pr_symbols(
    db: Session, pr: PullRequest, *, limit: int = 200
) -> list[CodeSymbol]:
    """PR 快照中、位于变更文件内的符号。

    注意：这是「touched-file 近似」——一个文件只要出现在 PR diff 里，其全部符号
    都会列出，并非精确的「增删改符号」清单。要做符号级 diff 需要 base/head 两份
    快照逐符号比较，当前 schema 没有 base 维度，暂不支持。
    """
    changed_paths = pr_changed_paths(pr)
    query = db.query(CodeSymbol).filter(
        CodeSymbol.repo_id == pr.repo_id, CodeSymbol.pr_id == pr.id
    )
    if changed_paths:
        query = query.filter(CodeSymbol.path.in_(changed_paths))
    return (
        query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc())
        .limit(limit)
        .all()
    )
