import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api.routes import chat as chat_routes
from app.db.models import (
    AgentRun,
    AgentWorkflowRun,
    Base,
    ChatMessage,
    Conversation,
    Repository,
)
from app.schemas.chat import ChatPlanExecuteRequest, ChatRequest
from app.services.chat_memory import MessageStore


def _db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _repo(db):
    repo = Repository(
        owner="kynflow", name="demo", full_name=f"kynflow/demo-{uuid.uuid4().hex[:8]}"
    )
    db.add(repo)
    db.flush()
    return repo


def _conversation(db, repo):
    conversation = Conversation(repo_id=repo.id, title="取消测试", status="active")
    db.add(conversation)
    db.flush()
    return conversation


# ---------------------------------------------------------------------------
# _persist_cancelled_run 单元测试
# ---------------------------------------------------------------------------


def test_persist_cancelled_run_writes_cancelled_agent_run_and_assistant_placeholder():
    db = _db_session()
    repo = _repo(db)
    conversation = _conversation(db, repo)
    message_store = MessageStore(db)
    user_message = message_store.append(
        repo_id=repo.id, conversation_id=conversation.id, role="user", content="hello"
    )
    db.commit()

    result = chat_routes._persist_cancelled_run(
        db,
        repo=repo,
        conversation=conversation,
        user_message=user_message,
        message_store=message_store,
        payload_message="hello",
    )

    runs = db.query(AgentRun).filter(AgentRun.status == "cancelled").all()
    assert len(runs) == 1
    assert runs[0].final_answer == "已停止响应。"
    assert runs[0].route == "cancelled"
    assistants = (
        db.query(ChatMessage).filter(ChatMessage.role == "assistant").all()
    )
    assert len(assistants) == 1
    assert assistants[0].content == "已停止响应。"
    assert assistants[0].meta.get("cancelled") is True
    assert result["run_id"] == str(runs[0].id)
    assert result["message_id"] == str(assistants[0].id)


# ---------------------------------------------------------------------------
# _check_cancellation 单元测试
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, disconnected: bool = False):
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.mark.asyncio
async def test_check_cancellation_raises_on_disconnect():
    with pytest.raises(chat_routes.ClientDisconnected):
        await chat_routes._check_cancellation(_FakeRequest(disconnected=True))


@pytest.mark.asyncio
async def test_check_cancellation_raises_on_stop_event():
    stop_event = asyncio.Event()
    stop_event.set()
    with pytest.raises(chat_routes.ClientDisconnected):
        await chat_routes._check_cancellation(_FakeRequest(), stop_event)


@pytest.mark.asyncio
async def test_check_cancellation_passes_when_connected():
    await chat_routes._check_cancellation(_FakeRequest(), asyncio.Event())


# ---------------------------------------------------------------------------
# _conversation_sse 断开集成测试
# ---------------------------------------------------------------------------


def _patch_sse_dependencies(monkeypatch, chat_agent):
    class FakeProgressiveContextManager:
        def __init__(self, db):
            pass

        async def ensure_headroom(self, repo, conversation, message):
            return {}

    class FakeEvidenceStore:
        def __init__(self, db):
            pass

        def rebuild_repo(self, repo, conversation):
            return None

    class FakeContextAssembler:
        def __init__(self, db):
            pass

        def assemble(self, repo, conversation, message):
            return SimpleNamespace(
                system_context="sys",
                history_messages=[],
                evidence=[],
                compression_stats={},
            )

    monkeypatch.setattr(
        chat_routes, "ProgressiveContextManager", FakeProgressiveContextManager
    )
    monkeypatch.setattr(chat_routes, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(chat_routes, "ContextAssembler", FakeContextAssembler)
    monkeypatch.setattr(chat_routes, "_build_conversation_tools", lambda *a, **k: {})
    monkeypatch.setattr(chat_routes, "_skill_runtime_context", dict)
    monkeypatch.setattr(chat_routes, "ChatAgent", lambda: chat_agent)


@pytest.mark.asyncio
async def test_conversation_sse_persists_cancelled_on_disconnect(monkeypatch):
    db = _db_session()
    repo = _repo(db)
    db.commit()

    class FakeAgent:
        async def run_stream(self, input_data, context):
            yield {"event": "thought", "data": {"content": "thinking"}}

    _patch_sse_dependencies(monkeypatch, FakeAgent())

    payload = ChatRequest(repo_id=repo.id, conversation_id=None, message="hello")
    gen = chat_routes._conversation_sse(payload, db, _FakeRequest(disconnected=True))

    events = [event async for event in gen]

    # 第一个 thought 事件在 yield 前被 _check_cancellation 拦截，不输出任何事件。
    assert events == []
    runs = db.query(AgentRun).filter(AgentRun.status == "cancelled").all()
    assert len(runs) == 1
    assistants = (
        db.query(ChatMessage).filter(ChatMessage.role == "assistant").all()
    )
    assert len(assistants) == 1
    assert assistants[0].content == "已停止响应。"
    # 用户消息有对应 assistant 占位，无孤儿。
    users = db.query(ChatMessage).filter(ChatMessage.role == "user").all()
    assert len(users) == 1


# ---------------------------------------------------------------------------
# _conversation_plan_execute_sse 取消收尾集成测试（P1）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_execute_sse_marks_workflow_cancelled(monkeypatch):
    db = _db_session()
    repo = _repo(db)
    db.commit()

    fake_report = {
        "spec": {"goal": "test goal", "trigger_message": "do the work", "tasks": []},
        "observation": {},
        "final_answer": "done",
        "metrics": {},
        "task_results": [],
        "citations": [],
        "agent_events": [],
        "tool_calls": [],
    }

    request = _FakeRequest(disconnected=False)

    def fake_persist_workflow(db, repo, conversation, report):
        workflow = AgentWorkflowRun(
            repo_id=repo.id,
            conversation_id=conversation.id,
            goal=str(report.get("spec", {}).get("goal") or ""),
            status="success",
        )
        db.add(workflow)
        db.flush()
        return workflow.id

    class FakeOrchestrator:
        def __init__(self, task_runners):
            pass

        async def run_spec(self, spec, workflow_context):
            # 模拟 run_spec 执行期间客户端断开。
            request.disconnected = True
            return fake_report

    _patch_sse_dependencies(monkeypatch, chat_agent=None)
    monkeypatch.setattr(
        chat_routes, "_build_engineering_workflow_runtime", lambda *a, **k: ({}, [])
    )
    monkeypatch.setattr(chat_routes, "WorkflowOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        chat_routes, "_persist_engineering_workflow", fake_persist_workflow
    )

    payload = ChatPlanExecuteRequest(
        repo_id=repo.id,
        conversation_id=None,
        message="do the work",
        workflow_spec={"goal": "test goal", "trigger_message": "do the work"},
    )
    gen = chat_routes._conversation_plan_execute_sse(payload, db, request)
    events = [event async for event in gen]

    # tool_use 事件在断开前已 yield，之后 delta/trace 阶段被拦截。
    assert any("event: tool_use" in event for event in events)
    workflows = db.query(AgentWorkflowRun).all()
    assert len(workflows) == 1
    assert workflows[0].status == "cancelled"
    runs = db.query(AgentRun).filter(AgentRun.status == "cancelled").all()
    assert len(runs) == 1
