"""
管理唤醒列表 LLM Tool

允许 LLM 管理当前会话的唤醒任务（会话隔离）。
"""

from typing import TYPE_CHECKING

from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api import logger

from ..utils import check_tool_permission, get_original_tool_name

if TYPE_CHECKING:
    from ..main import QQToolsPlugin


class WakeManageTool(FunctionTool):
    """管理唤醒列表工具 - 列出、删除、清空当前会话的唤醒任务"""
    
    def __init__(self, plugin: "QQToolsPlugin"):
        super().__init__(
            name="manage_wake",
            description="管理（列出、删除、清空）当前会话中的定时唤醒任务。",
            parameters={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["list", "delete", "clear"],
                        "description": "操作类型：list（列出所有待触发任务）、delete（删除指定任务）、clear（清空所有任务）"
                    },
                    "task_id": {
                        "type": "string",
                        "description": "要删除的任务ID（仅在 operation 为 delete 时需要）"
                    }
                },
                "required": ["operation"]
            }
        )
        self.plugin = plugin
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        
        # 权限检查
        permission_config = self.plugin.config.get("tool_permission", {})
        original_name = get_original_tool_name(self.name, self.plugin.add_tool_prefix)
        
        client = getattr(event, 'bot', None)
        has_permission, reason = await check_tool_permission(
            original_name,
            event,
            permission_config,
            client
        )
        
        if not has_permission:
            return reason
        
        # 获取参数
        operation = kwargs.get("operation")
        task_id = kwargs.get("task_id")
        
        # 参数校验
        if not operation:
            return "错误：必须指定操作类型（operation 参数）。"
        
        if operation not in ["list", "delete", "clear"]:
            return f"错误：不支持的操作类型 '{operation}'，可选值：list、delete、clear"
        
        # 获取当前会话 ID（用于会话隔离）
        session_id = event.unified_msg_origin
        
        # 检查调度器是否可用
        if not hasattr(self.plugin, 'wake_scheduler') or self.plugin.wake_scheduler is None:
            return "错误：唤醒调度器未初始化。"
        
        try:
            if operation == "list":
                return await self._handle_list(session_id)
            elif operation == "delete":
                return await self._handle_delete(session_id, task_id)
            elif operation == "clear":
                return await self._handle_clear(session_id)
        except Exception as e:
            logger.error(f"Failed to execute wake manage operation '{operation}': {e}")
            return f"操作失败: {e}"
    
    async def _handle_list(self, session_id: str) -> str:
        """列出当前会话的唤醒任务"""
        tasks = self.plugin.wake_scheduler.list_tasks(session_id=session_id)
        
        if not tasks:
            return "当前会话没有待触发的唤醒任务。"
        
        lines = [f"当前会话共有 {len(tasks)} 个待触发的唤醒任务：\n"]
        for i, task in enumerate(tasks, 1):
            lines.append(f"{i}. {task.format_display()}")
        
        return "\n".join(lines)
    
    async def _handle_delete(self, session_id: str, task_id: str) -> str:
        """删除指定的唤醒任务"""
        if not task_id:
            return "错误：删除操作需要指定 task_id 参数。"
        
        # 使用会话隔离删除（只能删除当前会话的任务）
        success = await self.plugin.wake_scheduler.delete_task(
            task_id=task_id,
            session_id=session_id  # 会话隔离
        )
        
        if success:
            return f"已成功删除唤醒任务 {task_id[:8]}..."
        else:
            # 检查任务是否存在但属于其他会话
            task = self.plugin.wake_scheduler.get_task(task_id)
            if task:
                return f"错误：无法删除任务 {task_id[:8]}...（该任务不属于当前会话）"
            else:
                return f"错误：未找到任务 {task_id[:8]}..."
    
    async def _handle_clear(self, session_id: str) -> str:
        """清空当前会话的所有唤醒任务"""
        count = await self.plugin.wake_scheduler.clear_tasks(session_id=session_id)
        
        if count == 0:
            return "当前会话没有待清空的唤醒任务。"
        else:
            return f"已清空当前会话的 {count} 个唤醒任务。"