"""通过工作台 MCP 获取可信版本的笔记，复用 SDK 模型并交给通用文档摄取用例。"""

import json
from hashlib import sha256

from pydantic import Field, JsonValue, TypeAdapter, ValidationError
from workstation_client.models.note_detail_response import NoteDetailResponse

from ...api.documents import IngestDocument
from ...application.documents import DocumentApplication
from ...safety import AllowSafeReadsPolicy, PermissionLevel
from ...tools.contracts import AsyncTool, ToolArguments, ToolDefinition, ToolError
from ...tools.execution import check_execution, execute_blocking
from ...tools.executor import ToolExecutor
from ...tools.mcp_remote import MCPResultTool, RemoteMCPTool
from ...tools.registry import ToolRegistry
from .config import WorkstationNotesConfig


class InspectNoteArguments(ToolArguments):
    """选择当前可信工作区的一篇笔记，不接受调用方提供正文、服务地址或工作区。"""

    note_id: int = Field(gt=0)


class IngestNoteArguments(InspectNoteArguments):
    """审批绑定已核对的来源版本，笔记在等待期间变化时必须重新确认。"""

    expected_version: int = Field(gt=0)


class WorkstationNoteSource:
    """保留工作台原文，检查 SDK 关键字段及身份；不把读取结果送给模型搬运。"""

    def __init__(self, binding: WorkstationNotesConfig, remote: RemoteMCPTool, *,
                 max_file_bytes: int, secrets: tuple[str | None, ...] = ()) -> None:
        self.binding: WorkstationNotesConfig = binding
        self.reader: MCPResultTool = MCPResultTool(remote)
        self.executor = ToolExecutor(ToolRegistry(), AllowSafeReadsPolicy())
        self.max_file_bytes = max_file_bytes
        self.secrets = secrets
        self.approval_context = sha256(
            f"workstation-sdk-5:{binding.server}:{binding.workspace_id}:{remote.definition.approval_context}".encode()
        ).hexdigest()
        if remote.permission != PermissionLevel.SAFE_READ:
            raise ValueError("工作台笔记读取工具必须明确配置为 read")
        properties = remote.definition.parameters.get("properties")
        if not isinstance(properties, dict) or not {"workspace_id", "note_id"}.issubset(properties):
            raise ValueError("工作台 get_note 需要新版 workspace_id 和 note_id 参数")
        remote.validate_arguments({"workspace_id": binding.workspace_id, "note_id": 1})

    def source_uri(self, note_id: int) -> str:
        """使用连接、业务工作区及笔记 ID 标识来源，标题更改不产生重复文档。"""
        return f"workstation://{self.binding.server}/workspaces/{self.binding.workspace_id}/notes/{note_id}"

    async def read(self, note_id: int) -> NoteDetailResponse:
        """经执行器授权后读取完整结构化结果，SDK 解析后再检查用于索引的关键字段。"""
        response = await self.executor.execute_value(self.reader, {
            "workspace_id": self.binding.workspace_id, "note_id": note_id,
        })
        payload = TypeAdapter(JsonValue).validate_python(response.structured_content)
        if not isinstance(payload, dict):
            raise ToolError("笔记缺少完整结构化结果", code="invalid_note")
        try:
            note = NoteDetailResponse.from_dict(payload)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise ToolError("笔记结果不符合工作台 SDK 5.0.0 契约", code="invalid_note") from None
        if (type(note.id) is not int or note.id != note_id or type(note.version) is not int or note.version < 1
                or not isinstance(note.title, str) or not note.title.strip() or len(note.title) > 200
                or not isinstance(note.content, str) or not note.content or "\0" in note.content
                or note.created_at.utcoffset() is None or note.updated_at.utcoffset() is None
                or any(value is not None and (type(value) is not int or value < 1)
                       for value in (note.folder_id, note.project_id))):
            raise ToolError("笔记身份、版本或正文格式无效", code="invalid_note")
        if len(note.content.encode("utf-8")) > self.max_file_bytes:
            raise ToolError("笔记正文超过摄取预算", code="resource_limit")
        if any(secret and (secret in note.content or secret in note.title) for secret in self.secrets):
            raise ToolError("笔记包含已知凭据，拒绝摄取", code="sensitive_content")
        return note

    def document(self, note: NoteDetailResponse) -> IngestDocument:
        """只映射检索需要的正文和来源元数据，不复制工作台的业务对象存储。"""
        metadata = {"provider": "workstation", "workspace_id": str(self.binding.workspace_id),
                    "note_id": str(note.id), "updated_at": note.updated_at.isoformat()}
        if note.folder_id is not None:
            metadata["folder_id"] = str(note.folder_id)
        if note.project_id is not None:
            metadata["project_id"] = str(note.project_id)
        return IngestDocument(source_uri=self.source_uri(note.id), title=note.title, text=note.content,
                              media_type="text/markdown", source_version=str(note.version), metadata=metadata)


class InspectWorkstationNoteTool:
    """读取可审批的来源摘要，不把整篇笔记放进模型上下文。"""

    permission = PermissionLevel.SAFE_READ

    def __init__(self, source: WorkstationNoteSource) -> None:
        self.source: WorkstationNoteSource = source
        self.definition = ToolDefinition("inspect_workstation_note",
            "Inspect a Workstation note before indexing: return its ID, title, version and size, without the body.",
            InspectNoteArguments.model_json_schema(), source.approval_context)

    def validate_arguments(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """仅接受正整数笔记 ID，不允许覆盖来源绑定。"""
        try:
            return InspectNoteArguments.model_validate(arguments).model_dump()
        except ValidationError:
            raise ToolError("笔记参数无效", code="invalid_arguments") from None

    async def ainvoke(self, arguments: dict[str, JsonValue]) -> str:
        """返回索引前可核对的版本及摘要，不修改工作台或本地知识库。"""
        data = InspectNoteArguments.model_validate(arguments)
        note = await self.source.read(data.note_id)
        return json.dumps({"note_id": note.id, "title": note.title, "version": note.version,
                           "source_uri": self.source.source_uri(note.id),
                           "bytes": len(note.content.encode("utf-8")),
                           "checksum": sha256(note.content.encode("utf-8")).hexdigest()}, ensure_ascii=False)


class IngestWorkstationNoteTool:
    """在整次工具调用获得写入审批后获取原文，再调用统一摄取入口。"""

    permission = PermissionLevel.CONTROLLED_WRITE

    def __init__(self, source: WorkstationNoteSource, documents: DocumentApplication) -> None:
        self.source: WorkstationNoteSource = source
        self.documents: DocumentApplication = documents
        self.definition = ToolDefinition("ingest_workstation_note",
            "Index or refresh a Workstation note by note_id and expected_version from inspect_workstation_note. "
            "Requires approval; reads the original body itself and never accepts model-generated content.",
            IngestNoteArguments.model_json_schema(), source.approval_context)

    def validate_arguments(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """审批范围包含笔记 ID 与来源版本，正文不属于可填写参数。"""
        try:
            return IngestNoteArguments.model_validate(arguments).model_dump()
        except ValidationError:
            raise ToolError("笔记 ID 或预期版本无效", code="invalid_arguments") from None

    async def ainvoke(self, arguments: dict[str, JsonValue]) -> str:
        """版本不符时停止，成功时只返回索引结果及来源，不返回笔记正文。"""
        data = IngestNoteArguments.model_validate(arguments)
        note = await self.source.read(data.note_id)
        if note.version != data.expected_version:
            raise ToolError("笔记已变化，请重新核对版本并审批", code="source_changed")
        check_execution()
        result = await execute_blocking(lambda: self.documents.ingest(self.source.document(note)))
        return json.dumps({**result, "source_uri": self.source.source_uri(note.id),
                           "source_version": note.version, "title": note.title}, ensure_ascii=False)


def workstation_note_tools(binding: WorkstationNotesConfig, remote: RemoteMCPTool,
                           documents: DocumentApplication, *, max_file_bytes: int,
                           secrets: tuple[str | None, ...] = ()) -> tuple[AsyncTool, ...]:
    """组装只读摘要和需审批摄取工具，具体 SDK 类型不进入 Agent 核心或 RAG。"""
    source = WorkstationNoteSource(binding, remote, max_file_bytes=max_file_bytes, secrets=secrets)
    return InspectWorkstationNoteTool(source), IngestWorkstationNoteTool(source, documents)
