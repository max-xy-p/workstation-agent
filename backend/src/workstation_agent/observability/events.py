"""提供事件收集和日志输出。"""

from __future__ import annotations

import json
import logging

from ..domain.events import AgentEvent


class CollectingEventSink:
    """将事件保存在内存中供检查。"""
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        """收集一条事件。"""
        self.events.append(event)


class LoggingEventSink:
    """将事件写入 JSON 日志；传入数据须预先脱敏。"""
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("workstation_agent.events")

    def emit(self, event: AgentEvent) -> None:
        """将一条事件写入日志。"""
        self.logger.info(
            json.dumps(
                {
                    "type": event.type.value,
                    "run_id": event.run_id,
                    "step_id": event.step_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "data": event.data,
                },
                ensure_ascii=False,
            )
        )
