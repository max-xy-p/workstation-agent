"""使用真实工作台 SDK 构造测试笔记，不复制工作台 DTO。"""

from datetime import UTC, datetime

from pydantic import JsonValue, TypeAdapter
from workstation_client.models.note_detail_response import NoteDetailResponse


def note_payload(*, content: str = "# 数据库\r\n工作台原文使用 PostgreSQL。", version: int = 7,
                 note_id: int = 42, title: str = "工作台资料") -> dict[str, JsonValue]:
    """使用当前 SDK 序列化笔记，转换只发生在外部 JSON 边界。"""
    note = NoteDetailResponse(id=note_id, title=title, content=content, version=version,
                              folder_id=None, folder_name=None, project_id=3, project_name="项目",
                              tags=[], created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    return TypeAdapter(dict[str, JsonValue]).validate_python(note.to_dict())
