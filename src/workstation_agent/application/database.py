"""提供显式数据库初始化和只读检查用例，所有操作经过工具权限边界。"""

from typing import Protocol

from pydantic import JsonValue

from ..safety import AllowSafeReadsPolicy, PermissionLevel
from ..tools.effects import authorized_operation


class DatabaseInitializer(Protocol):
    """声明业务数据库迁移及只读连接检查能力。"""

    def initialize(self) -> None: ...
    def check(self) -> bool: ...


class CheckpointInitializer(Protocol):
    """声明由官方组件执行检查点迁移的能力。"""

    def initialize(self) -> None: ...


class _InitializationPolicy(AllowSafeReadsPolicy):
    """仅允许显式 init-db 用例执行固定数据库初始化操作。"""

    def allows(self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]) -> bool:
        return tool_name == "database_initialize" and permission == PermissionLevel.CONTROLLED_WRITE


class DatabaseApplication:
    """将数据库初始化和健康检查作为明确用例提供给 CLI。"""

    def __init__(self, database: DatabaseInitializer, checkpoints: CheckpointInitializer) -> None:
        self.database: DatabaseInitializer = database
        self.checkpoints: CheckpointInitializer = checkpoints

    def initialize(self) -> None:
        """显式命令授权业务和官方检查点迁移，不创建或覆盖数据库本身。"""
        def migrate() -> None:
            self.database.initialize()
            self.checkpoints.initialize()

        authorized_operation("database_initialize", PermissionLevel.CONTROLLED_WRITE,
                             _InitializationPolicy(), migrate)

    def check(self) -> bool:
        """只读探测连接，不执行迁移。"""
        return authorized_operation("database_check", PermissionLevel.SAFE_READ,
                                    AllowSafeReadsPolicy(), self.database.check)
