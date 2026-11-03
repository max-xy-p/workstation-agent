"""以参数化 SQL 保存会话及文本历史，所有查询限定在可信身份范围内。"""

from contextlib import contextmanager
from collections.abc import Iterator
from typing import Literal

from psycopg import Connection
from pydantic import TypeAdapter

from ...api.conversations import Page, PageRequest
from ...domain.conversations import Conversation, ConversationMessage
from ...domain.sources import AnswerSource
from ...tools.contracts import ToolError
from .database import PostgresDatabase

_SESSION = TypeAdapter(Conversation)
_MESSAGE = TypeAdapter(ConversationMessage)


class PostgresConversationRepository:
    """在事务内锁住会话行，再保存历史及递增版本；不操作框架检查点。"""

    def __init__(self, database: PostgresDatabase, scope: str) -> None:
        self.database: PostgresDatabase = database
        self.scope = scope

    @contextmanager
    def _read(self) -> Iterator[Connection | None]:
        with self.database.transaction() as connection:
            if connection is not None:
                row = connection.execute("SELECT to_regclass('workstation.conversations')").fetchone()
                if row is None or row[0] is None:
                    connection = None
            yield connection

    def create(self, session_id: str, title: str) -> Conversation:
        """显式创建会话；已存在时保留其标题及历史。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO workstation.conversations(scope,id,title) VALUES (%s,%s,%s) "
                "ON CONFLICT (scope,id) DO NOTHING", (self.scope, session_id, title),
            )
            return self._session(connection, session_id)

    def _session(self, connection: Connection, session_id: str, *, lock: bool = False) -> Conversation:
        query = (
            "SELECT id,title,revision,created_at,updated_at FROM workstation.conversations "
            "WHERE scope=%s AND id=%s FOR UPDATE" if lock else
            "SELECT id,title,revision,created_at,updated_at FROM workstation.conversations "
            "WHERE scope=%s AND id=%s"
        )
        row = connection.execute(query, (self.scope, session_id)).fetchone()
        if row is None:
            raise ToolError("会话不存在", code="not_found")
        return _SESSION.validate_python(dict(zip(
            ("id", "title", "revision", "created_at", "updated_at"), row, strict=True,
        )))

    def get(self, session_id: str) -> Conversation:
        """读取当前范围的会话，其他身份的同名会话视为不存在。"""
        with self._read() as connection:
            if connection is None:
                raise ToolError("会话不存在", code="not_found")
            return self._session(connection, session_id)

    def list_sessions(self, page: PageRequest) -> Page[Conversation]:
        """按最近更新时间分页，多取一行判断是否还有下一页。"""
        with self._read() as connection:
            rows = [] if connection is None else connection.execute(
                "SELECT id,title,revision,created_at,updated_at FROM workstation.conversations "
                "WHERE scope=%s ORDER BY updated_at DESC,id LIMIT %s OFFSET %s",
                (self.scope, page.limit + 1, page.offset),
            ).fetchall()
            items = [_SESSION.validate_python(dict(zip(
                ("id", "title", "revision", "created_at", "updated_at"), row, strict=True,
            ))) for row in rows[:page.limit]]
            return Page(items=items, next_offset=page.offset + page.limit if len(rows) > page.limit else None)

    def _check(self, connection: Connection, session_id: str, revision: int | None) -> None:
        current = self._session(connection, session_id, lock=True)
        if revision is not None and current.revision != revision:
            raise ToolError("会话已变化，请重新读取后修改", code="revision_conflict")

    def _touch(self, connection: Connection, session_id: str) -> None:
        connection.execute(
            "UPDATE workstation.conversations SET revision=revision+1,updated_at=clock_timestamp() "
            "WHERE scope=%s AND id=%s", (self.scope, session_id),
        )

    def rename(self, session_id: str, title: str, revision: int) -> Conversation:
        """校验版本后修改标题，消息记录保持原样。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self._check(connection, session_id, revision)
            connection.execute(
                "UPDATE workstation.conversations SET title=%s WHERE scope=%s AND id=%s",
                (title, self.scope, session_id),
            )
            self._touch(connection, session_id)
            return self._session(connection, session_id)

    def _messages(self, connection: Connection, session_id: str) -> list[ConversationMessage]:
        rows = connection.execute(
            "SELECT id,session_id,position,role,content,run_id,created_at,updated_at,sources "
            "FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s ORDER BY position",
            (self.scope, session_id),
        ).fetchall()
        return [_MESSAGE.validate_python(dict(zip(
            ("id", "session_id", "position", "role", "content", "run_id", "created_at", "updated_at", "sources"),
            row, strict=True,
        ))) for row in rows]

    def history(self, session_id: str) -> list[ConversationMessage]:
        """为检查点重建读取完整文本历史，不读取工具正文。"""
        with self._read() as connection:
            if connection is None:
                raise ToolError("会话不存在", code="not_found")
            self._session(connection, session_id)
            return self._messages(connection, session_id)

    def list_messages(self, session_id: str, page: PageRequest) -> Page[ConversationMessage]:
        """在 SQL 中分页读取消息，避免列表接口加载整段历史。"""
        with self._read() as connection:
            if connection is None:
                raise ToolError("会话不存在", code="not_found")
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT id,session_id,position,role,content,run_id,created_at,updated_at,sources "
                "FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s "
                "ORDER BY position LIMIT %s OFFSET %s", (self.scope, session_id, page.limit + 1, page.offset),
            ).fetchall()
            items = [_MESSAGE.validate_python(dict(zip(
                ("id", "session_id", "position", "role", "content", "run_id", "created_at", "updated_at", "sources"),
                row, strict=True,
            ))) for row in rows[:page.limit]]
            return Page(items=items, next_offset=page.offset + page.limit if len(rows) > page.limit else None)

    def get_message(self, session_id: str, message_id: str) -> ConversationMessage:
        """按会话和消息双重归属查询，不允许通过消息 ID 跨会话读取。"""
        with self._read() as connection:
            if connection is None:
                raise ToolError("消息不存在", code="not_found")
            return self._message(connection, session_id, message_id)

    def _message(self, connection: Connection, session_id: str, message_id: str) -> ConversationMessage:
        row = connection.execute(
            "SELECT id,session_id,position,role,content,run_id,created_at,updated_at,sources "
            "FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s AND id=%s",
            (self.scope, session_id, message_id),
        ).fetchone()
        if row is None:
            raise ToolError("消息不存在", code="not_found")
        return _MESSAGE.validate_python(dict(zip(
            ("id", "session_id", "position", "role", "content", "run_id", "created_at", "updated_at", "sources"),
            row, strict=True,
        )))

    def append(
        self, session_id: str, message_id: str, role: Literal["user", "assistant"], content: str,
        run_id: str | None, revision: int | None = None,
        *, sources: list[AnswerSource] | None = None,
    ) -> ConversationMessage:
        """在会话行锁内追加文本，同一运行的重复回调不会重复保存。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self._check(connection, session_id, revision)
            inserted = connection.execute(
                "INSERT INTO workstation.conversation_messages(scope,session_id,id,role,content,run_id,sources) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING id",
                (self.scope, session_id, message_id, role, content, run_id,
                 TypeAdapter(list[AnswerSource]).dump_json(sources or []).decode()),
            ).fetchone()
            if inserted:
                self._touch(connection, session_id)
            return self._message(connection, session_id, message_id)

    def edit(self, session_id: str, message: ConversationMessage, content: str, revision: int) -> ConversationMessage:
        """修改用户消息并删除后续文本，两项变更在同一事务提交。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self._check(connection, session_id, revision)
            connection.execute(
                "DELETE FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s AND position>%s",
                (self.scope, session_id, message.position),
            )
            connection.execute(
                "UPDATE workstation.conversation_messages SET content=%s,run_id=NULL,updated_at=clock_timestamp() "
                "WHERE scope=%s AND session_id=%s AND id=%s", (content, self.scope, session_id, message.id),
            )
            self._touch(connection, session_id)
            return self._message(connection, session_id, message.id)

    def truncate(self, session_id: str, position: int, revision: int) -> None:
        """删除指定位置及后续文本，防止留下失去用户问题的答复。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self._check(connection, session_id, revision)
            connection.execute(
                "DELETE FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s AND position>=%s",
                (self.scope, session_id, position),
            )
            self._touch(connection, session_id)

    def delete(self, session_id: str, revision: int) -> None:
        """删除会话，数据库外键同时删除其全部文本。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self._check(connection, session_id, revision)
            connection.execute(
                "DELETE FROM workstation.runs WHERE workspace_id=%s AND scope=%s AND session_id=%s",
                (self.database.workspace_id, self.scope, session_id),
            )
            connection.execute("DELETE FROM workstation.conversations WHERE scope=%s AND id=%s", (self.scope, session_id))

    def clear(self, session_id: str) -> None:
        """供显式 CLI 清除命令删除文本；会话不存在时无需创建记录。"""
        with self._read() as connection:
            exists = connection is not None and connection.execute(
                "SELECT 1 FROM workstation.conversations WHERE scope=%s AND id=%s", (self.scope, session_id),
            ).fetchone() is not None
        if exists:
            self.truncate(session_id, 0, self.get(session_id).revision)
