"""
主动唤醒 LLM Tool

允许 LLM 在当前会话创建定时唤醒任务。
"""

from typing import TYPE_CHECKING

from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api import logger

from ..utils import check_tool_permission, get_original_tool_name

if TYPE_CHECKING:
    from ..main import QQToolsPlugin


class WakeScheduleTool(FunctionTool):
    """主动唤醒工具 - 创建定时唤醒任务"""
    
    def __init__(self, plugin: "QQToolsPlugin"):
        super().__init__(
            name="schedule",
            description="创建定时唤醒任务。在延迟指定时间（秒）后唤醒机器人，以便在同一会话中继续处理任务或发送提醒。",
            parameters={
                "type": "object",
                "properties": {
                    "time": {
                        "type": "integer",
                        "description": "从现在起延迟多少秒后触发唤醒。例如：60 表示 1 分钟后，3600 表示 1 小时后。"
                    },
                    "remark": {
                        "type": "string",
                        "description": "可选的备注信息，唤醒时会携带此备注，帮助你记住这个唤醒任务的目的。"
                    }
                },
                "required": ["time"]
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
        delay_time = kwargs.get("time")
        remark = kwargs.get("remark")
        
        # 参数校验
        if delay_time is None:
            return "错误：必须指定延迟时间（time 参数）。"
        
        try:
            delay_time = int(delay_time)
        except (ValueError, TypeError):
            return f"错误：延迟时间必须是整数（秒），收到: {delay_time}"
        
        if delay_time <= 0:
            return "错误：延迟时间必须大于 0 秒。"
        
        if delay_time > 86400 * 365:  # 最大 1 年
            return "错误：延迟时间不能超过 1 年（31536000 秒）。"
        
        # 获取会话信息
        session_id = event.unified_msg_origin
        platform_id = event.get_platform_id()
        
        # 检查调度器是否可用
        if not hasattr(self.plugin, 'wake_scheduler') or self.plugin.wake_scheduler is None:
            return "错误：唤醒调度器未初始化。"
        
        try:
            # 创建唤醒任务
            task_id = await self.plugin.wake_scheduler.create_task(
                session_id=session_id,
                platform_id=platform_id,
                delay_seconds=delay_time,
                remark=remark
            )
            
            # 获取任务信息用于返回
            task = self.plugin.wake_scheduler.get_task(task_id)
            if task:
                trigger_time_str = task.trigger_time_str()
                remaining_str = self._format_duration(delay_time)
                
                result = f"唤醒任务创建成功！\n"
                result += f"- 任务ID: {task_id}\n"
                result += f"- 触发时间: {trigger_time_str}\n"
                result += f"- 延迟: {remaining_str}"
                if remark:
                    result += f"\n- 备注: {remark}"
                
                return result
            else:
                return f"唤醒任务已创建，任务ID: {task_id}"
                
        except Exception as e:
            logger.error(f"Failed to create wake task: {e}")
            return f"创建唤醒任务失败: {e}"
    
    def _format_duration(self, seconds: int) -> str:
        """格式化时间间隔"""
        if seconds >= 86400:
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            return f"{days}天{hours}小时" if hours else f"{days}天"
        elif seconds >= 3600:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
        elif seconds >= 60:
            minutes = seconds // 60
            secs = seconds % 60
            return f"{minutes}分钟{secs}秒" if secs else f"{minutes}分钟"
        else:
            return f"{seconds}秒"