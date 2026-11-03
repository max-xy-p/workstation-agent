"""从成功检索的工具消息提取回答来源，不信任模型自行编写的来源列表。"""

from collections.abc import Callable

from langchain_core.messages import ToolMessage
from pydantic import TypeAdapter, ValidationError

from ..context.evidence import Evidence
from ..domain.sources import AnswerSource, source_id
from ..tools.executor import ToolResult
from .rag import SearchResult

_RESULT = TypeAdapter(ToolResult)
_SEARCH = TypeAdapter(SearchResult)


def collect_sources(
    message: ToolMessage, existing: list[AnswerSource], *,
    resolve_url: Callable[[str], str | None] | None = None,
    secrets: tuple[str | None, ...] = (),
) -> list[AnswerSource]:
    """只接收完整的本地检索结果，按文档版本与原文范围去重，每次运行最多保存 32 个片段。"""
    if message.name != "search_documents" or message.status == "error" or not isinstance(message.content, str):
        return existing
    try:
        result = _RESULT.validate_json(message.content)
        if not result.success or result.tool_name != "search_documents":
            return existing
        search = _SEARCH.validate_json(result.content)
    except ValidationError:
        # 被输出预算截断的 JSON 不是完整证据，不能猜测或补齐来源。
        return existing
    if search["status"] != "ok":
        return existing
    sources = {item.id: item for item in existing}
    for evidence in search["evidence"]:
        if len(sources) >= 32:
            break
        if not _valid(evidence, secrets):
            continue
        identifier = source_id(evidence["document_id"], evidence["checksum"], evidence["source_start"], evidence["source_end"])
        sources.setdefault(identifier, AnswerSource(
            id=identifier, document_id=evidence["document_id"], checksum=evidence["checksum"],
            source_uri=evidence["source_uri"], title=evidence["title"], line_start=evidence["line_start"],
            line_end=evidence["line_end"], content=evidence["content"], citation=evidence["citation"],
            source_version=evidence.get("source_version"),
            original_url=resolve_url(evidence["source_uri"]) if resolve_url else None,
        ))
    return list(sources.values())


def _valid(evidence: Evidence, secrets: tuple[str | None, ...]) -> bool:
    """限制片段大小与原文位置，并防止已知凭据进入回答历史。"""
    encoded = TypeAdapter(Evidence).dump_json(evidence)
    text_fields = (evidence["content"], evidence["title"], evidence["source_uri"], evidence["citation"],
                   evidence.get("source_version") or "")
    return (
        len(encoded) <= 32_000 and evidence["line_start"] >= 1
        and evidence["line_end"] >= evidence["line_start"] and evidence["source_start"] >= 0
        and evidence["source_end"] - evidence["source_start"] == len(evidence["content"])
        and bool(evidence["content"]) and len(evidence["checksum"]) == 64
        and not any(secret and any(secret in value for value in text_fields) for secret in secrets)
    )
