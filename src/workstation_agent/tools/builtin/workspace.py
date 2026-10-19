"""提供列目录和读文件两个工具，访问目标必须位于配置的工作区内。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, field_validator

from ...safety.permissions import PermissionLevel
from ..contracts import PydanticTool, ToolArguments, ToolError
from ..execution import check_execution


class WorkspacePathArguments(ToolArguments):
    """定义文件工具共用的非空路径参数。"""

    # 目标路径，解析后必须位于工作区内。
    path: str = Field(min_length=1, pattern=r"^[^\x00]+$")

    @field_validator("path")
    @classmethod
    def non_blank_path(cls, value: str) -> str:
        """拒绝纯空白路径。"""
        if not value.strip():
            raise ValueError("path must not be blank")
        return value


class ListFilesArguments(WorkspacePathArguments):
    """定义目录列表参数，默认目录为工作区根目录。"""

    # 待列举目录，默认点号表示工作区根目录。
    path: str = Field(default=".", min_length=1, pattern=r"^[^\x00]+$")


class ReadTextFileArguments(WorkspacePathArguments):
    """按字符范围读取文件；版本不匹配时拒绝拼接已改变的文件。"""

    offset: int = Field(default=0, ge=0, le=100_000_000)
    limit: int | None = Field(default=None, ge=1, le=50_000)
    expected_version: str | None = Field(default=None, max_length=100)


def resolve_inside(workspace: Path, relative_path: str) -> Path:
    """解析真实路径并拒绝工作区外的目标；不隔离并发路径替换。"""
    workspace = workspace.resolve()
    candidate = (workspace / relative_path).resolve()
    if not candidate.is_relative_to(workspace):
        raise ToolError("path is outside the configured workspace", code="path_outside_workspace")
    return candidate


class ListFilesTool(PydanticTool[ListFilesArguments]):
    """列出工作区内指定目录的直接子项。"""

    name = "list_files"
    description = "List files and directories inside the workspace."
    permission = PermissionLevel.SAFE_READ
    arguments_model = ListFilesArguments

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def run(self, arguments: ListFilesArguments) -> str:
        """返回目录子项的名称和类型。"""
        path = resolve_inside(self.workspace, arguments.path)
        if not path.is_dir():
            raise ToolError("requested directory does not exist", code="not_found")
        entries = []
        for item in path.iterdir():
            check_execution()
            if item.is_symlink() or item.is_junction():
                kind = "link"
            else:
                kind = "directory" if item.is_dir() else "file"
            entries.append({"name": item.name, "type": kind})
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
        return json.dumps(entries, ensure_ascii=False)


class ReadTextFileTool(PydanticTool[ReadTextFileArguments]):
    """读取工作区内的 UTF-8 文件并限制内容长度。"""

    name = "read_text_file"
    description = (
        "Read a UTF-8 file inside the workspace. Use offset/limit to page; "
        "when truncated, follow next_offset with expected_version from the result."
    )
    permission = PermissionLevel.SAFE_READ
    arguments_model = ReadTextFileArguments

    def __init__(self, workspace: Path, max_chars: int = 50_000) -> None:
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        self.workspace = workspace.resolve()
        self.max_chars = max_chars

    def run(self, arguments: ReadTextFileArguments) -> str:
        """按字符读取；分段请求在读取前后核对文件时间和大小，避免混合不同版本。"""
        path = resolve_inside(self.workspace, arguments.path)
        if not path.is_file():
            raise ToolError("requested file does not exist", code="not_found")
        before = path.stat()
        version = f"{before.st_mtime_ns}-{before.st_size}"
        if arguments.expected_version is not None and arguments.expected_version != version:
            raise ToolError("文件已变化，请从头重新读取", code="source_changed")
        limit = min(arguments.limit or self.max_chars, max(1, self.max_chars - 160))
        try:
            with path.open("r", encoding="utf-8") as stream:
                remaining = arguments.offset
                while remaining:
                    check_execution()
                    skipped = stream.read(min(remaining, 8192))
                    if not skipped:
                        break
                    remaining -= len(skipped)
                check_execution()
                content = stream.read(limit + 1)
        except UnicodeDecodeError:
            raise ToolError("file is not valid UTF-8 text", code="invalid_utf8") from None
        after = path.stat()
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            raise ToolError("读取期间文件已变化，请重新读取", code="source_changed")
        if len(content) > limit:
            marker = f"\n[next_offset={arguments.offset + limit}; version={version}]\n[truncated]"
            return content[:limit] + marker
        return content
