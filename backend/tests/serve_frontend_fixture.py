"""启动浏览器联调用的真实 HTTP 服务，以离线模型替代外部推理；必须使用专用测试数据库。"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from langchain_core.messages import AIMessage

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_execution_control import CountingTool
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.bootstrap import MemoryApprovalPolicy, database_for
from workstation_agent.config import Settings
from workstation_agent.interfaces.agent_http import build_agent_service
from workstation_agent.safety import AllowSafeReadsPolicy
from workstation_agent.storage.postgres.checkpoints import PostgresSessionBackend
from workstation_agent.tools.checkpoints import AuthorizedCheckpointer
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry


def serve() -> None:
    """显式启动临时工作区服务，不加载真实模型配置，也不读写个人工作区文件。"""
    token = os.getenv('WORKSTATION_AGENT_E2E_TOKEN')
    if not token or not os.getenv('WORKSTATION_AGENT_DATABASE_URL'):
        raise SystemExit('请配置专用测试数据库和 WORKSTATION_AGENT_E2E_TOKEN')
    port = int(os.getenv('WORKSTATION_AGENT_HTTP_PORT', '18766'))
    with TemporaryDirectory() as directory:
        settings = Settings(Path(directory), agent_service_port=port, agent_service_token=token)

        @asynccontextmanager
        async def application() -> AsyncIterator[AgentApplication]:
            """保留真实数据库、权限与检查点，仅使用确定的模型回复和计数测试工具。"""
            saver = AuthorizedCheckpointer(
                PostgresSessionBackend(database_for(settings)), settings.memory_access,
                MemoryApprovalPolicy(sessions=True),
            )
            tools = ToolApplication(ToolExecutor(ToolRegistry((CountingTool(),)), AllowSafeReadsPolicy()))
            model = ScriptedModel(responses=[call_message('count', {}), AIMessage(content='后端联调完成')])
            yield AgentApplication(build_agent(model, local_tools(tools), checkpointer=saver), checkpointer=saver)

        uvicorn.run(build_agent_service(settings, transport='local', application_factory=application),
                    host='127.0.0.1', port=port, access_log=False, log_level='critical')


if __name__ == '__main__':
    serve()
