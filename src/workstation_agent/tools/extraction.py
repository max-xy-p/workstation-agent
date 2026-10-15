"""有界自动提取：候选无落库权限，提交再次经过独立写入授权。"""

from pydantic import Field

from ..memory.extraction import prepare_candidates
from ..memory.service import validate_content
from ..safety import PermissionLevel
from .contracts import PydanticTool, ToolArguments, ToolError
from .effects import authorized_operation


class ExtractArguments(ToolArguments):
    """内部输入只包含本次用户文本和由应用生成的来源标识。"""

    text: str = Field(min_length=1)
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


class MemoryExtractionTool(PydanticTool[ExtractArguments]):
    """内部工具不进入模型可见目录，禁止自动删除或跨范围修改。"""

    name = "extract_memory"
    description = "Internal bounded long-term memory extraction."
    permission = PermissionLevel.SENSITIVE
    arguments_model = ExtractArguments

    def __init__(self, service, access, extractor, settings, policy):
        self.service, self.access, self.extractor = service, access, extractor
        self.settings, self.policy = settings, policy

    def run(self, args):
        import json

        scope = self.access.scope()
        if len(args.text) > self.settings.extract_chars:
            raise ToolError("提取输入超过预算，未截断事实", code="resource_limit")
        validate_content(args.text, self.service.secrets)

        def snapshot():
            if self.service.repository.processed(scope, args.run_id):
                return None
            generation = self.service.repository.generation(scope)
            # 先限定当前写入范围，不向提取模型暴露其他范围的记录。
            known = [
                r for r in self.service.search(self.access, args.text, limit=20) if r.scope == scope
            ]
            existing, budget = [], self.settings.extract_chars - len(args.text)
            for record in known:
                cost = len(record.content) + len(record.id) + 100
                if cost <= budget:
                    existing.append(record)
                    budget -= cost
            return generation, existing

        current = authorized_operation(
            "memory_snapshot", PermissionLevel.SAFE_READ, self.policy, snapshot
        )
        if current is None:
            return json.dumps({"status": "already_processed", "saved": 0})
        generation, existing = current
        proposed = self.extractor.extract(args.text, tuple(existing))
        if len(proposed) > self.settings.extract_limit:
            raise ToolError("提取候选超过预算", code="resource_limit")
        records, expected, pending = prepare_candidates(
            proposed,
            existing,
            scope,
            args.text,
            args.run_id,
            args.session_id,
        )
        saved = authorized_operation(
            "memory_commit_extraction",
            PermissionLevel.CONTROLLED_WRITE,
            self.policy,
            lambda: self.service.commit(
                self.access,
                tuple(records),
                expected,
                scope,
                generation=generation,
                batch_id=args.run_id,
            ),
        )
        return json.dumps(
            {"status": "completed", "saved": len(records) if saved else 0, "pending": pending}
        )
