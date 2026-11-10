"""维护 Workstation MCP 公开工具的业务分组和常用搜索别名。"""

from ...tools.discovery import DiscoveryEntry
from ...tools.mcp_config import MCPServerConfig
from ...tools.mcp_remote import public_tool_name

WORKSTATION_GROUPS: dict[str, str] = {
    "overview": "工作台概览和服务健康",
    "workspaces": "工作区列表、详情和设置",
    "workspaces.members": "成员查询、邀请和权限管理",
    "workspaces.audit": "审计事件",
    "notes": "笔记查询、正文、编辑和导入",
    "notes.history": "笔记历史版本、回收站和恢复",
    "notes.taxonomy": "笔记文件夹和标签",
    "projects": "项目查询、编辑和状态",
    "projects.development": "项目开发信息、仓库绑定和工作区开发动态",
    "tasks": "任务查询、编辑、状态及 GitHub issue 工单关联",
    "tasks.issue_sources": "关联 GitHub issue 工单、创建任务和确认来源",
    "integrations": "外部集成、连接验证和启停",
    "integrations.sync": "外部仓库发现与导入、同步任务和结果",
    "integrations.webhooks": "GitHub webhook 配置和投递重试",
}

_TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "overview": (
        "health_check",
        "get_dashboard",
    ),
    "workspaces": (
        "list_workspaces",
        "get_workspace",
        "update_workspace",
    ),
    "workspaces.members": (
        "list_workspace_members",
        "find_workspace_member_candidate",
        "add_workspace_member",
        "update_workspace_member",
        "remove_workspace_member",
        "list_workspace_invitations",
        "revoke_workspace_invitation",
    ),
    "workspaces.audit": (
        "list_audit_events",
    ),
    "notes": (
        "list_notes",
        "get_note",
        "create_note",
        "update_note",
        "import_notes",
        "delete_note",
    ),
    "notes.history": (
        "list_deleted_notes",
        "restore_note",
        "permanently_delete_note",
        "list_note_revisions",
        "get_note_revision",
        "restore_note_revision",
    ),
    "notes.taxonomy": (
        "list_note_folders",
        "get_note_folder",
        "create_note_folder",
        "update_note_folder",
        "delete_note_folder",
        "list_tags",
        "get_tag",
        "create_tag",
        "update_tag",
        "delete_tag",
    ),
    "projects": (
        "list_projects",
        "get_project",
        "create_project",
        "update_project",
        "update_project_status",
        "delete_project",
    ),
    "projects.development": (
        "list_project_repositories",
        "bind_project_repository",
        "unbind_project_repository",
        "get_project_development",
        "list_project_development_items",
        "list_workspace_development",
    ),
    "tasks": (
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "update_task_status",
        "delete_task",
    ),
    "tasks.issue_sources": (
        "list_task_issue_sources",
        "create_task_from_issue",
        "link_task_issue",
        "unlink_task_issue",
        "confirm_task_issue_source",
    ),
    "integrations": (
        "list_integration_connections",
        "create_integration_connection",
        "update_integration_connection",
        "delete_integration_connection",
        "validate_integration_connection",
        "set_integration_connection_enabled",
    ),
    "integrations.sync": (
        "discover_integration_repositories",
        "list_integration_repositories",
        "import_integration_repositories",
        "trigger_integration_sync",
        "list_integration_runs",
        "get_integration_run",
        "list_integration_run_items",
    ),
    "integrations.webhooks": (
        "get_github_webhook",
        "configure_github_webhook",
        "list_github_webhook_deliveries",
        "retry_github_webhook_delivery",
    ),
}
_GROUP_BY_TOOL = {name: group for group, names in _TOOL_GROUPS.items() for name in names}
_ALIASES: dict[str, tuple[str, ...]] = {
    "list_notes": ("查找笔记", "按标题搜索笔记"),
    "get_note": ("读原文", "查看完整笔记"),
    "get_task": ("核对已有任务", "任务详情"),
    "link_task_issue": ("关联已有 issue", "绑定 GitHub 工单"),
    "create_task_from_issue": ("从 issue 创建新任务",),
    "unlink_task_issue": ("解除关联", "解绑工单"),
    "trigger_integration_sync": ("同步仓库", "刷新外部数据"),
    "list_note_revisions": ("查看编辑历史", "笔记版本"),
}


def workstation_discovery_entries(server: MCPServerConfig) -> dict[str, DiscoveryEntry]:
    """为可信配置中允许的工具建立目录，缺少分类时在启动阶段提示维护。"""
    unknown = sorted(set(server.tools) - _GROUP_BY_TOOL.keys())
    if unknown:
        raise ValueError("工作台工具分组缺少白名单条目，请先维护分组目录")
    return {
        public_tool_name(server.name, name): DiscoveryEntry(_GROUP_BY_TOOL[name], permission, _ALIASES.get(name, ()))
        for name, permission in server.tools.items()
    }
