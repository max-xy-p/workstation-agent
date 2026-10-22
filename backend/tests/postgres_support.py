"""查询当前测试工作区的数据，避免把 PostgreSQL 文件存在性当成 PostgreSQL 行为。"""

from workstation_agent.bootstrap import database_for
from workstation_agent.config import Settings


def has_memory(settings: Settings) -> bool:
    """检查当前工作区是否写入记忆，不创建表或读取正文。"""
    with database_for(settings).transaction() as connection:
        if connection is None:
            return False
        return connection.execute(
            "SELECT 1 FROM workstation.memory_records WHERE workspace_id=%s LIMIT 1",
            (settings.memory_access.workspace_id,),
        ).fetchone() is not None


def has_rag(settings: Settings) -> bool:
    """检查当前工作区是否发布索引，不执行数据库初始化。"""
    with database_for(settings).transaction() as connection:
        if connection is None:
            return False
        return connection.execute(
            "SELECT 1 FROM workstation.rag_documents WHERE workspace_id=%s LIMIT 1",
            (settings.memory_access.workspace_id,),
        ).fetchone() is not None
