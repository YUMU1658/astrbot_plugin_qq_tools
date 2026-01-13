from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name

class GroupBanTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="group_ban",
            description="在群聊中禁言（口球）指定群成员。需要机器人有管理员或群主权限。仅在群聊中可用。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标QQ号",
                    },
                    "duration": {
                        "type": "integer",
                        "description": "禁言时间（秒）。0为解除禁言。",
                    }
                },
                "required": ["qq_id", "duration"],
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        
        # 权限检查
        if self.plugin:
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
        
        qq_id = kwargs.get("qq_id")
        duration = kwargs.get("duration")
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "仅在群聊中才可以禁言。"

        group_id = event.message_obj.group_id
        client = event.bot
        
        # 转换角色名称为中文
        role_map = {
            "owner": "群主",
            "admin": "管理员",
            "member": "成员"
        }

        try:
            # 1. 获取机器人自己的身份
            login_info = await call_onebot(client, 'get_login_info')
            bot_id = str(login_info.get('user_id'))
            
            bot_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(bot_id), no_cache=True)
            bot_role = bot_member_info.get('role', 'member')
            
            # 2. 获取对方的身份
            target_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(qq_id), no_cache=True)
            target_role = target_member_info.get('role', 'member')
            
            bot_role_cn = role_map.get(bot_role, bot_role)
            target_role_cn = role_map.get(target_role, target_role)

            # 3. 检查权限
            can_ban = False
            if bot_role == 'owner':
                # 群主可以禁言 管理员 和 成员
                if target_role != 'owner':
                    can_ban = True
            elif bot_role == 'admin':
                # 管理员只能禁言 成员
                if target_role == 'member':
                    can_ban = True
            
            # 如果是解除禁言(duration=0)，通常逻辑相同，或者是禁言的逆操作
            
            if not can_ban:
                return f"禁言失败：权限不足。你的身份：{bot_role_cn}，对方的身份：{target_role_cn}"
            
            # 4. 执行禁言
            await call_onebot(client, 'set_group_ban', group_id=group_id, user_id=int(qq_id), duration=int(duration))
            
            action_str = "解除禁言" if duration == 0 else f"禁言 {duration} 秒"
            return f"已对 QQ:{qq_id} 执行{action_str}。"

        except Exception as e:
            logger.error(f"Group ban failed: {e}")
            return f"操作失败: {e}"