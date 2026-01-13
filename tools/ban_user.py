import time
import asyncio
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api import logger
from ..utils import check_tool_permission, get_original_tool_name

class BanUserTool(FunctionTool):
    def __init__(self, plugin):
        super().__init__(
            name="ban_user",
            description="将指定用户加入 AstrBot 黑名单，使其无法与机器人交互。支持设置拉黑时长（秒），-1为永久。",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "目标用户ID"
                    },
                    "duration": {
                        "type": "integer",
                        "description": "拉黑时长(秒)。-1为永久，0为解除拉黑。",
                        "default": -1
                    }
                },
                "required": ["user_id"]
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
        
        user_id = kwargs.get("user_id")
        duration = kwargs.get("duration", -1)
        
        ban_list = self.plugin.config.get("ban_list", [])
        
        # 解除拉黑
        if duration == 0:
            # 找到并移除
            new_list = [u for u in ban_list if u.get("user_id") != user_id]
            if len(new_list) == len(ban_list):
                return f"用户 {user_id} 不在黑名单中。"
            
            self.plugin.config["ban_list"] = new_list
            # 使用 run_in_executor 避免同步 IO 阻塞事件循环
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.plugin.config.save_config)
            return f"已解除用户 {user_id} 的拉黑。"

        # 执行拉黑
        ban_data = {
            "user_id": user_id,
            "ban_time": int(time.time()),
            "duration": duration
        }

        # 加入列表 (先移除旧的)
        new_list = [u for u in ban_list if u.get("user_id") != user_id]
        new_list.append(ban_data)
        
        self.plugin.config["ban_list"] = new_list
        # 使用 run_in_executor 避免同步 IO 阻塞事件循环
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.plugin.config.save_config)

        dur_str = "永久" if duration == -1 else f"{duration}秒"
        return f"已将用户 {user_id} 拉黑。时长: {dur_str}"