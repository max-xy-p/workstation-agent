"""提供列目录和读文件两个工具，访问目标必须位于配置的工作区内。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, field_validator

from ...safety.permissions import PermissionLevel
from ..contracts import PydanticTool, ToolArguments, ToolError


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
    """定义文件读取所需的路径参数。"""


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
    description = "Read a UTF-8 text file inside the workspace."
    permission = PermissionLevel.SAFE_READ
    arguments_model = ReadTextFileArguments

    def __init__(self, workspace: Path, max_chars: int = 50_000) -> None:
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        self.workspace = workspace.resolve()
        self.max_chars = max_chars

    def run(self, arguments: ReadTextFileArguments) -> str:
        """返回文件文本，超限时截断。"""
        path = resolve_inside(self.workspace, arguments.path)
        if not path.is_file():
            raise ToolError("requested file does not exist", code="not_found")
        try:
            with path.open("r", encoding="utf-8") as stream:
                content = stream.read(self.max_chars + 1)
        except UnicodeDecodeError:
            raise ToolError("file is not valid UTF-8 text", code="invalid_utf8") from None
        if len(content) > self.max_chars:
            return content[: self.max_chars] + "\n[truncated]"
        return content
