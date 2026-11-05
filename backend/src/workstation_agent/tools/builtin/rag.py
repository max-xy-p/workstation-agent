"""声明 RAG 工具权限，所有文档与模型副作用均发生在授权后。"""

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from ...application.rag import RagApplication
from ...api.documents import IngestDocument
from ...safety.permissions import PermissionLevel
from ..contracts import PreparedCall, PydanticTool, ToolArguments, ToolError
from ..executor import ToolExecutor
from .workspace import WorkspacePathArguments, resolve_inside


class IngestArguments(WorkspacePathArguments):
    """定义单文档摄取及过滤标签。"""

    metadata: dict[str, str] = Field(default_factory=dict)


class SearchArguments(ToolArguments):
    """定义有界查询与精确文档过滤。"""

    query: str = Field(min_length=1, max_length=2000)
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=6, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def non_blank(cls, value):
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class ModelArguments(ToolArguments):
    """内部模型操作不进入 Agent 可见的工具目录。"""

    operation: Literal["embed", "contextualize", "rerank"]
    texts: list[str] = Field(default_factory=list, max_length=32)
    title: str = ""
    background: str = ""
    content: str = ""
    query: str = ""


class RagModelTool(PydanticTool[ModelArguments]):
    """内部敏感工具，出站授权通过同一执行器策略检查。"""

    name = "rag_model"
    description = "Execute a configured RAG model operation."
    permission = PermissionLevel.SENSITIVE
    arguments_model = ModelArguments

    def __init__(self, backend):
        self.backend = backend

    def run(self, arguments):
        if arguments.operation == "embed":
            result = self.backend.embed(arguments.texts)
        elif arguments.operation == "contextualize":
            result = self.backend.contextualize(
                arguments.title, arguments.background, arguments.content
            )
        else:
            result = self.backend.rerank(arguments.query, arguments.texts)
        return json.dumps(result, ensure_ascii=False, allow_nan=False)


class AuthorizedBackend:
    """把核心模型 Protocol 调用转交内部 ToolExecutor，不直接访问网络。"""

    def __init__(self, backend, executor: ToolExecutor):
        self.fingerprint = backend.fingerprint
        self.executor = executor

    def _call(self, operation, **kwargs):
        result = self.executor.execute("rag_model", {"operation": operation, **kwargs})
        if not result.success:
            raise ToolError(
                "RAG model operation failed", code=result.error_code or "execution_failed"
            )
        return json.loads(result.content)

    def embed(self, texts):
        return self._call("embed", texts=texts)

    def contextualize(self, title, background, content):
        return self._call("contextualize", title=title, background=background, content=content)

    def rerank(self, query, texts):
        return self._call("rerank", query=query, texts=texts)


class IngestDocumentTool(PydanticTool[IngestArguments]):
    """读取完整有界 UTF-8 文档，处理成功后发布索引。"""

    name = "ingest_document"
    description = "Index or update a workspace Markdown/text document; requires write approval."
    permission = PermissionLevel.CONTROLLED_WRITE
    arguments_model = IngestArguments

    def __init__(self, workspace: Path, application: RagApplication, *, secrets: tuple[str | None, ...] = ()) -> None:
        self.workspace = workspace.resolve()
        self.application = application
        self.secrets = secrets

    def prepare_call(self, arguments: dict[str, JsonValue]) -> PreparedCall:
        """预读有界原文并固定内容，审批只展示来源与版本摘要。"""
        return self._prepare(self._parse(arguments))

    def _prepare(self, arguments: IngestArguments) -> PreparedCall:
        path = resolve_inside(self.workspace, arguments.path)
        if path.suffix.lower() not in {".md", ".txt"}:
            raise ToolError("only .md and .txt are supported", code="unsupported_format")
        if not path.is_file():
            raise ToolError("requested file does not exist", code="not_found")
        maximum = self.application.settings.max_file_bytes
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ToolError("RAG file size limit exceeded", code="resource_limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("file is not valid UTF-8", code="invalid_utf8") from None
        if "\0" in text:
            raise ToolError("binary text is not supported", code="unsupported_format")
        source = path.relative_to(self.workspace).as_posix()
        values = (source, text, *arguments.metadata.keys(), *arguments.metadata.values())
        if any(secret and secret in value for secret in self.secrets for value in values):
            raise ToolError("文档包含已知凭据，拒绝摄取", code="sensitive_content")
        plan = self.application.plan_ingestion(source, text, arguments.metadata)
        return PreparedCall(
            {"path": source, "action": plan.action, "checksum": plan.document.checksum,
             "index_revision": plan.revision, "bytes": len(raw)},
            sha256(f"{self.workspace}:{plan.fingerprint}".encode()).hexdigest(),
            lambda: json.dumps(self.application.commit_ingestion(plan), ensure_ascii=False),
            unchanged=plan.action == "unchanged",
        )

    def run(self, arguments: IngestArguments) -> str:
        """执行已获授权的摄取；执行器使用准备阶段返回的同一份计划。"""
        return self._prepare(arguments).commit()


class IngestExternalDocumentTool(PydanticTool[IngestDocument]):
    """只供应用调用的摄取工具，正文来自用户或来源适配器，不加入模型可发现目录。"""

    name = "ingest_external_document"
    description = "Index an explicitly supplied external text document."
    permission = PermissionLevel.CONTROLLED_WRITE
    arguments_model = IngestDocument

    def __init__(self, application: RagApplication, *, secrets: tuple[str | None, ...] = ()) -> None:
        self.application: RagApplication = application
        self.secrets = secrets

    def run(self, arguments: IngestDocument) -> str:
        values = (arguments.source_uri, arguments.title, arguments.text, arguments.source_version or "",
                  *arguments.metadata.keys(), *arguments.metadata.values())
        if any(secret and secret in value for secret in self.secrets for value in values):
            raise ToolError("文档包含已知凭据，拒绝摄取", code="sensitive_content")
        metadata = dict(arguments.metadata)
        if arguments.source_version is not None:
            metadata["source_version"] = arguments.source_version
        return json.dumps(self.application.ingest(
            arguments.source_uri, arguments.text, metadata,
            title=arguments.title, media_type=arguments.media_type,
        ), ensure_ascii=False)


class SearchDocumentsTool(PydanticTool[SearchArguments]):
    """只读检索原文，通过统一 Context 证据格式输出引用。"""

    name = "search_documents"
    description = (
        "Search indexed documents, return original evidence and line citations. "
        "Use another query only for missing evidence; do not repeat identical searches. "
        "Derived context is not factual evidence; report missing evidence."
    )
    permission = PermissionLevel.SAFE_READ
    arguments_model = SearchArguments

    def __init__(self, application: RagApplication) -> None:
        self.application = application

    def run(self, arguments: SearchArguments) -> str:
        return json.dumps(
            self.application.search(arguments.query, arguments.filters, arguments.limit),
            ensure_ascii=False,
            allow_nan=False,
        )


class DeleteDocumentTool(PydanticTool[WorkspacePathArguments]):
    """删除索引及派生数据，保留来源文件。"""

    name = "delete_document"
    description = "Delete a document from the RAG index; requires write approval."
    permission = PermissionLevel.CONTROLLED_WRITE
    arguments_model = WorkspacePathArguments

    def __init__(self, workspace: Path, application: RagApplication) -> None:
        self.workspace = workspace.resolve()
        self.application = application

    def run(self, arguments: WorkspacePathArguments) -> str:
        path = resolve_inside(self.workspace, arguments.path)
        source = path.relative_to(self.workspace).as_posix()
        return json.dumps(self.application.delete(source), ensure_ascii=False)


class RagStatusArguments(ToolArguments):
    """索引健康检查不接受额外参数。"""


class RagStatusTool(PydanticTool[RagStatusArguments]):
    """只读检查索引版本和记录数量，不发送文档内容。"""

    name = "rag_status"
    description = "Check RAG index health and counts without contacting models."
    permission = PermissionLevel.SAFE_READ
    arguments_model = RagStatusArguments

    def __init__(self, application: RagApplication) -> None:
        self.application = application

    def run(self, arguments: RagStatusArguments) -> str:
        return json.dumps(self.application.status(), ensure_ascii=False)
