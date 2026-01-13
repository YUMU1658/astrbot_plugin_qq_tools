from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import get_qq_string_length, truncate_qq_string, call_onebot, check_tool_permission, get_original_tool_name

class ChangeGroupCardTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="change_group_card",
            description="修改指定群成员（或机器人自己）在当前群的群名片（群昵称）。修改他人名片需要管理员权限。",
            parameters={
                "type": "object",
                "properties": {
                    "card": {
                        "type": "string",
                        "description": "新的群名片(昵称)。",
                    },
                    "qq_id": {
                        "type": "string",
                        "description": "目标QQ号。如果不填，则修改机器人自己的群名片。",
                    },
                },
                "required": ["card"],
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        card = kwargs.get("card")
        qq_id = kwargs.get("qq_id")
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"
        
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

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "请在群聊中使用此功能。"

        group_id = event.message_obj.group_id
        sender_id = str(event.get_sender_id())
        client = event.bot

        # 获取机器人自己的QQ号
        bot_id = None
        try:
            login_info = await call_onebot(client, 'get_login_info')
            bot_id = str(login_info.get('user_id'))
        except Exception as e:
            logger.error(f"Failed to get bot login info: {e}")
            # Fallback: try to guess or proceed?
            # If we can't get bot_id, we can't verify if target is bot, so we might default to needing permissions.
            pass

        # Determine target user
        if qq_id:
            target_id = str(qq_id)
        else:
            if bot_id:
                target_id = bot_id
            else:
                return "无法获取机器人QQ号，请显式提供 qq_id 参数。"

        # Permission check
        # 1. Modifying self (Sender == Target): Allowed (usually)
        # 2. Modifying bot (Target == Bot): Allowed (Sender asks Bot to change Bot's card)
        # 3. Modifying others (Sender != Target AND Target != Bot): Requires Admin/Owner role for Sender
        
        is_modifying_self = (target_id == sender_id)
        is_modifying_bot = (bot_id and target_id == bot_id)
        
        if not is_modifying_self and not is_modifying_bot:
            try:
                # Get sender's role
                member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(sender_id))
                role = member_info.get('role', 'member')
                
                if role not in ['admin', 'owner']:
                    return f"修改失败：你不是群主或管理员，无法修改他人(QQ: {target_id})的群名片。"
            except Exception as e:
                logger.error(f"Failed to get member info for permission check: {e}")
                return f"权限检查失败：无法获取你的群员信息 ({e})。"

        # Execute modification
        try:
            # 1. 获取原群名片
            old_card_name = "未知"
            try:
                target_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(target_id), no_cache=True)
                old_card_name = target_info.get('card') or target_info.get('nickname') or str(target_id)
            except Exception as e:
                logger.warning(f"Failed to get old card info: {e}")

            # 2. 处理新群名片 (截断)
            real_card = truncate_qq_string(card)
            is_truncated = (real_card != card)
            current_len = get_qq_string_length(real_card)

            # 3. 修改
            await call_onebot(client, 'set_group_card', group_id=group_id, user_id=int(target_id), card=real_card)
            
            # 4. 构建返回值
            # 成功从“xxx”修改为“yyy”，字数n/60
            ret_msg = f"成功从“{old_card_name}”修改为“{real_card}”，字数{current_len}/60"
            
            if is_truncated:
                ret_msg += "。注意：原输入超出字数上限，已自动截断保存。"
                
            return ret_msg

        except Exception as e:
            logger.error(f"Failed to set group card: {e}")
            return f"修改群名片失败: {e} (可能是机器人权限不足)"