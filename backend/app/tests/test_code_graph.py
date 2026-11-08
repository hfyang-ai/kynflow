import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.models import Base, CodeSymbol, PRFile, PullRequest, Repository
from app.services.agents.chat_agent import ChatAgent
from app.tools.code_graph import (
    find_symbol_tool,
    outline_tool,
    pr_changed_symbols_tool,
    read_symbol_tool,
)
from app.tools.context import ToolContext

SERVICE_PY = (
    "line1\n"
    "line2\n"
    "def handler():\n"
    "    return 1\n"
    "\n"
    "def other():\n"
    "    pass\n"
)


def _db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _repo_with_checkout(db, tmp_path):
    repo = Repository(
        owner="local",
        name="demo",
        full_name="local/demo",
        checkout_mode="managed",
        clone_url="https://github.com/local/demo.git",
    )
    db.add(repo)
    db.flush()
    project = tmp_path / "repo"
    project.mkdir()
    (project / "service.py").write_text(SERVICE_PY, encoding="utf-8")
    repo.local_path = str(project)
    return repo, project


def _ctx(db, repo):
    return ToolContext(repo=repo, conversation=None, db=db, mcp_client=None)


def _add_symbol(db, repo, *, path, name, kind="function", start, end, pr_id=None, pr_number=None):
    db.add(
        CodeSymbol(
            repo_id=repo.id,
            path=path,
            name=name,
            kind=kind,
            language="python",
            start_line=start,
            end_line=end,
            branch="main",
            commit_sha="abc123",
            pr_id=pr_id,
            pr_number=pr_number,
        )
    )
    db.flush()


@pytest.mark.asyncio
async def test_read_symbol_returns_body_and_read_range(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="service.py", name="handler", start=3, end=4)
    _add_symbol(db, repo, path="service.py", name="other", start=6, end=7)

    result = await read_symbol_tool(
        _ctx(db, repo), {"path": "service.py", "name": "handler"}, {}
    )

    assert result["route"] == "code_read_symbol"
    assert "3: def handler():" in result["answer"]
    assert "4:     return 1" in result["answer"]
    assert result["read_range"] == {
        "path": "service.py",
        "start_line": 3,
        "end_line": 4,
        "total_lines": 7,
        "truncated": True,
        "next_start_line": 5,
    }
    assert result["tool_calls"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_read_symbol_same_name_lists_candidates(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="service.py", name="run", kind="method", start=3, end=4)
    _add_symbol(db, repo, path="service.py", name="run", kind="method", start=8, end=9)

    result = await read_symbol_tool(
        _ctx(db, repo), {"path": "service.py", "name": "run"}, {}
    )

    assert "2 个名为「run」的符号" in result["answer"]
    assert "kind" in result["answer"]
    assert "read_range" not in result


@pytest.mark.asyncio
async def test_read_symbol_missing_symbol_guides_outline(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)

    result = await read_symbol_tool(
        _ctx(db, repo), {"path": "service.py", "name": "nope"}, {}
    )

    assert "未在 service.py 找到名为「nope」" in result["answer"]
    assert "code.outline" in result["answer"]


@pytest.mark.asyncio
async def test_outline_returns_symbol_map(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="service.py", name="handler", start=3, end=4)
    _add_symbol(db, repo, path="service.py", name="other", start=6, end=7)

    result = await outline_tool(_ctx(db, repo), {"path": "service.py"}, {})

    assert result["route"] == "code_outline"
    assert '"name": "handler"' in result["answer"]
    assert '"name": "other"' in result["answer"]
    assert '"start": 3' in result["answer"]


@pytest.mark.asyncio
async def test_find_symbol_returns_all_same_name_definitions(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="a.py", name="run", start=1, end=2)
    _add_symbol(db, repo, path="b.py", name="run", start=5, end=6)

    result = await find_symbol_tool(_ctx(db, repo), {"name": "run"}, {})

    assert result["route"] == "code_find_symbol"
    assert '"path": "a.py"' in result["answer"]
    assert '"path": "b.py"' in result["answer"]
    assert len(result["citations"]) == 2


@pytest.mark.asyncio
async def test_find_symbol_prefix_and_contains_match(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="a.py", name="runLoop", start=1, end=2)
    _add_symbol(db, repo, path="b.py", name="runOnce", start=1, end=2)
    _add_symbol(db, repo, path="c.py", name="run", start=1, end=2)

    prefix = await find_symbol_tool(
        _ctx(db, repo), {"name": "run", "match": "prefix"}, {}
    )
    assert '"name": "runLoop"' in prefix["answer"]
    assert '"name": "runOnce"' in prefix["answer"]
    assert '"name": "run"' in prefix["answer"]
    assert len(prefix["citations"]) == 3

    contains = await find_symbol_tool(
        _ctx(db, repo), {"name": "Once", "match": "contains"}, {}
    )
    assert '"name": "runOnce"' in contains["answer"]
    assert len(contains["citations"]) == 1


@pytest.mark.asyncio
async def test_find_symbol_exact_does_not_match_substring(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    _add_symbol(db, repo, path="a.py", name="runLoop", start=1, end=2)
    _add_symbol(db, repo, path="b.py", name="run", start=1, end=2)

    result = await find_symbol_tool(_ctx(db, repo), {"name": "run"}, {})

    assert '"name": "run"' in result["answer"]
    assert "runLoop" not in result["answer"]
    assert len(result["citations"]) == 1


@pytest.mark.asyncio
async def test_pr_changed_symbols_lists_touched_file_symbols(tmp_path) -> None:
    db = _db_session()
    repo, _ = _repo_with_checkout(db, tmp_path)
    pr = PullRequest(
        repo_id=repo.id,
        number=12,
        title="t",
        state="open",
        created_at=datetime.now(UTC),
    )
    db.add(pr)
    db.flush()
    db.add(PRFile(pr_id=pr.id, filename="service.py", status="modified"))
    _add_symbol(
        db, repo, path="service.py", name="handler", start=3, end=4,
        pr_id=pr.id, pr_number=12,
    )
    _add_symbol(db, repo, path="other.py", name="unrelated", start=1, end=2, pr_id=pr.id, pr_number=12)

    result = await pr_changed_symbols_tool(_ctx(db, repo), {"pr_number": 12}, {})

    assert result["route"] == "code_pr_changed_symbols"
    assert "touched-file" in result["answer"]
    assert '"name": "handler"' in result["answer"]
    assert "unrelated" not in result["answer"]
    assert result["citations"][0]["id"] == str(pr.id)


def test_record_read_coverage_accepts_read_symbol_range() -> None:
    agent = ChatAgent()
    state = {"read_coverage": {}}
    observation = {
        "read_range": {"path": "service.py", "start_line": 3, "end_line": 4}
    }

    agent._record_read_coverage(
        state, "code.read_symbol", {"path": "service.py", "name": "handler"}, observation
    )

    assert state["read_coverage"]["service.py"] == [[3, 4]]


def test_record_read_coverage_ignores_tools_without_range() -> None:
    agent = ChatAgent()
    state = {"read_coverage": {}}

    agent._record_read_coverage(
        state, "code.outline", {"path": "service.py"}, {}
    )

    assert state["read_coverage"] == {}
