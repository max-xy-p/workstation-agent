"""为用户正文和未来的来源适配器提供统一摄取用例，不获取外部数据。"""

from pydantic import TypeAdapter

from ..api.documents import IngestDocument
from ..tools.contracts import ToolError
from .rag import IngestResult
from .tools import ToolApplication


class DocumentApplication:
    """将完整正文交给内部授权工具，再由现有 RAG 管线处理和保存。"""

    def __init__(self, tools: ToolApplication) -> None:
        self.tools: ToolApplication = tools

    def ingest(self, document: IngestDocument) -> IngestResult:
        """写入一个来源的索引版本；不向 Agent 暴露任意正文写入工具。"""
        result = self.tools.execute_tool("ingest_external_document", document.model_dump())
        if not result.success:
            raise ToolError("文档摄取失败", code=result.error_code or "ingestion_failed")
        return TypeAdapter(IngestResult).validate_json(result.content)
