"""作用域绑定的记忆工具，不接受模型传入用户或项目身份。"""

from contextvars import ContextVar
from datetime import datetime
from typing import Literal

from pydantic import Field

from ...domain.runs import new_id
from ...memory.models import MemoryAccess, MemorySource
from ...memory.service import MemoryService
from ...safety import PermissionLevel
from ..contracts import PydanticTool, Tool, ToolArguments, ToolError

MEMORY_SOURCE: ContextVar[MemorySource | None] = ContextVar("memory_source", default=None)


class RememberArguments(ToolArguments):
    """只接受正文、类别和当前身份可用的范围。"""

    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["preference", "decision", "experience"] = "preference"
    scope: Literal["user", "workspace", "project"] | None = None
    expires_at: str | None = None


class UpdateArguments(ToolArguments):
    """更新要求读取时的版本，避免并发静默覆盖。"""

    memory_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=4000)
    expires_at: str | None = None


class IdArguments(ToolArguments):
    """查看只需 ID，但服务仍检查所有权。"""

    memory_id: str = Field(min_length=1, max_length=128)


class DeleteArguments(IdArguments):
    """删除也要求预期版本。"""

    expected_version: int = Field(ge=1)


class SearchArguments(ToolArguments):
    """检索数量有界，不允许请求扩大范围。"""

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class ListArguments(ToolArguments):
    """支持有界分页，避免一次输出全部私人记忆。"""

    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


def _source() -> MemorySource:
    return MEMORY_SOURCE.get() or MemorySource(origin="explicit", run_id=new_id("manual"))


def _expiry(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value and value != "never" else None
        if parsed is not None and parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise ToolError(
            "过期时间必须为带时区的 ISO 时间或 never", code="invalid_arguments"
        ) from None


def memory_tools(service: MemoryService, access: MemoryAccess) -> tuple[Tool, ...]:
    """生成只绑定当前可信范围的记忆工具目录。"""

    class RememberTool(PydanticTool[RememberArguments]):
        """保存自包含笔记，完全相同的有效正文合并来源。"""

        name = "remember_memory"
        description = "Save a self-contained long-term note; requires explicit write approval."
        permission = PermissionLevel.CONTROLLED_WRITE
        arguments_model = RememberArguments

        def run(self, arguments: RememberArguments) -> str:
            record = service.remember(
                access,
                content=arguments.content,
                kind=arguments.kind,
                scope=access.scope(arguments.scope),
                source=_source(),
                expires_at=_expiry(arguments.expires_at),
            )
            return record.model_dump_json()

    class UpdateTool(PydanticTool[UpdateArguments]):
        """修改指定笔记，原来源保留，正文整体替换。"""

        name = "update_memory"
        description = "Replace a note using its current version; preserve relevant conditions."
        permission = PermissionLevel.CONTROLLED_WRITE
        arguments_model = UpdateArguments

        def run(self, arguments: UpdateArguments) -> str:
            record = service.update(
                access,
                arguments.memory_id,
                arguments.expected_version,
                content=arguments.content,
                source=_source(),
                expiry="keep" if arguments.expires_at is None else _expiry(arguments.expires_at),
            )
            return record.model_dump_json()

    class GetTool(PydanticTool[IdArguments]):
        """查看单条记忆，过期记录也可用于人工维护。"""

        name = "get_memory"
        description = "Read one owned memory, including expired notes."
        permission = PermissionLevel.SAFE_READ
        arguments_model = IdArguments

        def run(self, arguments: IdArguments) -> str:
            return service.get(access, arguments.memory_id).model_dump_json()

    class SearchTool(PydanticTool[SearchArguments]):
        """返回相关的有效记忆，原文由 Context 统一组装。"""

        name = "search_memory"
        description = "Search relevant active long-term notes within the configured identity."
        permission = PermissionLevel.SAFE_READ
        arguments_model = SearchArguments

        def run(self, arguments: SearchArguments) -> str:
            from ...context.builder import serialize_memory_records

            return serialize_memory_records(
                service.search(access, arguments.query, limit=arguments.limit)
            )

    class ListTool(PydanticTool[ListArguments]):
        """按范围列出记录，返回总数便于分页。"""

        name = "list_memories"
        description = "List owned notes, including expired notes, with bounded pagination."
        permission = PermissionLevel.SAFE_READ
        arguments_model = ListArguments

        def run(self, arguments: ListArguments) -> str:
            import json

            records = service.records(access, include_expired=True)
            return json.dumps(
                {
                    "total": len(records),
                    "records": [
                        r.model_dump(mode="json")
                        for r in records[arguments.offset : arguments.offset + arguments.limit]
                    ],
                },
                ensure_ascii=False,
            )

    class DeleteTool(PydanticTool[DeleteArguments]):
        """删除正文并提升水位，不删除原始会话。"""

        name = "delete_memory"
        description = (
            "Forget one memory and invalidate in-flight extraction; destructive approval required."
        )
        permission = PermissionLevel.DESTRUCTIVE
        arguments_model = DeleteArguments

        def run(self, arguments: DeleteArguments) -> str:
            import json

            service.forget(access, arguments.memory_id, arguments.expected_version)
            return json.dumps({"deleted": arguments.memory_id, "session_cleared": False})

    return (RememberTool(), UpdateTool(), GetTool(), SearchTool(), ListTool(), DeleteTool())
