"""复用官方连接池，按进程配置共享连接并在进程退出时关闭。"""

import atexit
from threading import RLock

from psycopg import Connection
from psycopg.rows import DictRow, TupleRow, dict_row
from psycopg_pool import ConnectionPool


def _reset[Row](connection: Connection[Row]) -> None:
    """归还连接前清除只读和 schema 设置，避免影响下一次业务操作。"""
    connection.execute("RESET ALL")
    if not connection.autocommit:
        connection.commit()


class DatabasePools:
    """分别保留元组行和官方检查点字典行的连接池，保留驱动类型信息。"""

    def __init__(self, url: str) -> None:
        self.records: ConnectionPool[Connection[TupleRow]] = ConnectionPool(
            url, connection_class=Connection[TupleRow],
            kwargs={"connect_timeout": 5, "options": "-c statement_timeout=30000 -c lock_timeout=5000"},
            min_size=0, max_size=4, timeout=5, open=False, reset=_reset, name="workstation-records",
        )
        self.checkpoints: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            url, connection_class=Connection[DictRow],
            kwargs={"connect_timeout": 5, "autocommit": True, "row_factory": dict_row,
                    "prepare_threshold": 0, "options": "-c statement_timeout=30000 -c lock_timeout=5000"},
            min_size=0, max_size=4, timeout=5, open=False, reset=_reset, name="workstation-checkpoints",
        )
        self.records.open()
        self.checkpoints.open()

    def close(self) -> None:
        """停止连接池工作线程并关闭空闲连接。"""
        self.records.close()
        self.checkpoints.close()


_POOLS: dict[str, DatabasePools] = {}
_LOCK = RLock()


def pools_for(url: str) -> DatabasePools:
    """第一次执行数据库操作时创建池，不在应用构造时建立连接。"""
    with _LOCK:
        if url not in _POOLS:
            _POOLS[url] = DatabasePools(url)
        return _POOLS[url]


@atexit.register
def close_pools() -> None:
    """进程结束时关闭自身创建的全部连接池。"""
    with _LOCK:
        for pool in _POOLS.values():
            pool.close()
        _POOLS.clear()
