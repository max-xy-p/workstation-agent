"""适配标准 MCP 工具结果和参数，只增加本项目的权限、文本预算和脱敏规则。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.types import CallToolResult, EmbeddedResource, RequestParamsMeta, TextContent, TextResourceContents, Tool
from pydantic import JsonValue, TypeAdapter

from ..memory.service import redact
from ..safety import AllowSafeReadsPolicy, PermissionLevel, PermissionRequired
from .contracts import AsyncTool, ToolDefinition, ToolError
from .execution import EXECUTION, GRANT
from .executor import ToolExecutor, ToolResult
from .registry import ToolRegistry

_JSON = TypeAdapter(JsonValue)
_RESULT = TypeAdapter(ToolResult)


def public_tool_name(server: str, remote: str) -> str:
    """用服务名前缀区分同名工具，并遵守模型工具名称的长度与字符限制。"""
    import re

    readable = re.sub(r"[^A-Za-z0-9_-]", "_", server + "__" + remote)
    if readable == server + "__" + remote and len(readable) <= 64:
        return readable
    digest = hashlib.sha256((server + ":" + remote).encode()).hexdigest()[:12]
    return readable[:51] + "_" + digest


def _local_refs(value: JsonValue) -> bool:
    """远端 schema 只能引用自身，避免参数校验触发未授权的外部网络读取。"""
    if isinstance(value, dict):
        return all((key not in {"$ref", "$dynamicRef"} or isinstance(item, str) and item.startswith("#"))
                   and _local_refs(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_local_refs(item) for item in value)
    return True


def _clean(value: JsonValue, secrets: tuple[str | None, ...]) -> JsonValue:
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    return value


def _contains_secret(value: JsonValue, secrets: tuple[str | None, ...]) -> bool:
    """拒绝把远端 schema 中的已知凭据作为参数默认值或说明暴露给模型。"""
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret(key, secrets) or _contains_secret(item, secrets) for key, item in value.items())
    return False


class RemoteMCPTool:
    """绑定一个已发现的工具和可信本地权限；不使用远端读写注解自动授权。"""

    def __init__(self, client: Client, tool: Tool, name: str, permission: PermissionLevel, *,
                 internal_service: bool = False, secrets: tuple[str | None, ...] = (),
                 approval_context: str | None = None) -> None:
        self.client: Client = client
        self.remote_name = tool.name
        self.permission = permission
        self.internal_service = internal_service
        self.secrets = secrets
        schema = deepcopy(tool.input_schema)
        if _contains_secret(_JSON.validate_python(schema), secrets):
            raise ValueError("MCP 工具 schema 包含已知凭据")
        if not _local_refs(_JSON.validate_python(schema)):
            raise ValueError("MCP 工具 schema 不支持外部引用")
        Draft202012Validator.check_schema(schema)
        self.validator: Draft202012Validator = Draft202012Validator(schema)
        self.definition = ToolDefinition(name, redact(tool.description or tool.name, secrets), schema, approval_context)

    def validate_arguments(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """在授权前按已发现的 JSON Schema 校验参数，不回显失败值。"""
        if not self.validator.is_valid(arguments):
            raise ToolError("远程工具参数无效", code="invalid_arguments")
        return arguments

    async def ainvoke(self, arguments: dict[str, JsonValue]) -> str:
        """调用固定远程名称；内部服务才接收本项目的审批和截止时间元数据。"""
        return self._content(await self.call_result(arguments))

    async def call_result(self, arguments: dict[str, JsonValue]) -> CallToolResult:
        """返回官方 MCP 结果供授权包装器处理，不对原始文档执行截断或改写。"""
        meta: RequestParamsMeta = {}
        if self.internal_service:
            grant, control = GRANT.get(), EXECUTION.get()
            if grant is not None:
                meta["workstation_approval"] = grant.model_dump(mode="json")
            if control is not None:
                meta["workstation_deadline"] = control.deadline
        return await self.client.call_tool(self.remote_name, arguments, meta=meta or None)

    def _content(self, response: CallToolResult) -> str:
        if self.internal_service:
            result = _RESULT.validate_json(json.dumps(response.structured_content))
            if result.tool_name != self.remote_name or response.is_error != (not result.success):
                raise ToolError("内部 MCP 返回不一致的结果", code="invalid_output")
            if not result.success:
                raise ToolError(redact(result.error or "远程工具失败", self.secrets),
                                code=result.error_code or "remote_tool_failed")
            return redact(result.content, self.secrets)
        if response.is_error:
            # 外部服务可能在异常文本中泄露后端凭据；不直接向模型转发原始异常。
            raise ToolError("外部 MCP 工具返回执行错误", code="remote_tool_failed")
        if response.structured_content is not None:
            return json.dumps(_clean(_JSON.validate_python(response.structured_content), self.secrets), ensure_ascii=False)
        texts: list[str] = []
        for block in response.content:
            if isinstance(block, TextContent):
                texts.append(block.text)
            elif isinstance(block, EmbeddedResource) and isinstance(block.resource, TextResourceContents):
                texts.append(block.resource.text)
            else:
                raise ToolError("当前 Agent 仅接受 MCP 文本和结构化结果", code="unsupported_content")
        return redact("\n".join(texts), self.secrets)


class MCPResultTool:
    """在工具执行器内读取完整 MCP 响应，供来源适配器校验原文，不进入模型工具目录。"""

    def __init__(self, remote: RemoteMCPTool) -> None:
        self.remote: RemoteMCPTool = remote
        self.definition = remote.definition
        self.permission = remote.permission

    def validate_arguments(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """沿用已发现工具的参数 schema，不另建业务参数校验器。"""
        return self.remote.validate_arguments(arguments)

    async def ainvoke(self, arguments: dict[str, JsonValue]) -> CallToolResult:
        """只返回成功响应；远端错误不携带原始异常文本。"""
        response = await self.remote.call_result(arguments)
        if response.is_error:
            raise ToolError("读取外部资料失败", code="remote_tool_failed")
        return response


def remote_invocation(tool: AsyncTool, max_output_chars: int) -> Callable[[dict[str, JsonValue]], Awaitable[ToolResult]]:
    """复用统一执行器在远程副作用前审批，供 LangChain 工具和应用调用。"""
    executor = ToolExecutor(ToolRegistry(), AllowSafeReadsPolicy(), max_output_chars=max_output_chars)

    async def invoke(arguments: dict[str, JsonValue]) -> ToolResult:
        try:
            return await executor.execute_async(tool, arguments)
        except PermissionRequired:
            return ToolResult(tool.definition.name, "该工具需要审批", False, "该工具需要审批", "permission_required")

    return invoke
