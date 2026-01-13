from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name

class SendGroupNoticeTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="send_group_notice",
            description="发布群公告。仅在群聊中可用，且需要机器人是管理员或群主。",
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "公告内容。支持使用 \\n 换行。",
                    }
                },
                "required": ["content"],
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        content = kwargs.get("content")
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "仅在群聊中才可以发送公告。"
        
        # 权限检查
        if self.plugin:
            permission_config = self.plugin.config.get("tool_permission", {})
            original_name = get_original_tool_name(self.name, self.plugin.add_tool_prefix)
            
            client = event.bot
            has_permission, reason = await check_tool_permission(
                original_name,
                event,
                permission_config,
                client
            )
            
            if not has_permission:
                return reason

        group_id = event.message_obj.group_id
        client = event.bot
        
        # 转换换行符
        content = content.replace("\\n", "\n")

        try:
            # 1. 获取机器人自己的身份
            login_info = await call_onebot(client, 'get_login_info')
            bot_id = str(login_info.get('user_id'))
            
            bot_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(bot_id), no_cache=True)
            bot_role = bot_member_info.get('role', 'member')
            
            # 2. 检查权限 (只有群主和管理员可以发公告)
            if bot_role not in ['owner', 'admin']:
                return f"发送公告失败：权限不足。机器人当前身份为 {bot_role}，需要 admin 或 owner 权限。"
            
            # 3. 发送公告
            # NapCat/Go-CQHTTP API: _send_group_notice
            # 参数: group_id, content, image (可选)
            await call_onebot(client, '_send_group_notice', group_id=group_id, content=content)
            
            return f"已发送群公告。"

        except Exception as e:
            logger.error(f"Send group notice failed: {e}")
            return f"发送公告失败: {e}"