"""会话清除用例，不把内部检查点格式暴露给接口。"""


class SessionApplication:
    """仅操作当前配置身份下的会话。"""

    def __init__(self, checkpointer):
        self.checkpointer = checkpointer

    def clear(self, session_id):
        """清除框架历史和摘要，长期记忆需要另行遗忘。"""
        config = self.checkpointer.config(session_id)
        self.checkpointer.delete_thread(config["configurable"]["thread_id"])
        return {"cleared": session_id, "long_term_memory_deleted": False}
