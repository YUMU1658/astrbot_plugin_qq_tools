from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name

class KickUserTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="kick_user",
            description="将指定群成员移出本群（踢出群聊）。需要机器人有管理员或群主权限。仅在群聊中可用。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "要移出群聊的目标用户QQ号",
                    },
                    "reject_add_request": {
                        "type": "boolean",
                        "description": "是否拒绝此人的再次加群请求。默认为 false。",
                        "default": False
                    }
                },
                "required": ["qq_id"],
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
        reject_add_request = kwargs.get("reject_add_request", False)
        
        if not qq_id:
            return "错误：必须提供要移出的目标用户QQ号 (qq_id)。"
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "仅在群聊中才可以移出成员。"

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
            try:
                target_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(qq_id), no_cache=True)
                target_role = target_member_info.get('role', 'member')
                target_nickname = target_member_info.get('card') or target_member_info.get('nickname') or str(qq_id)
            except Exception as e:
                # 如果获取不到目标用户信息，可能用户不在群里
                return f"获取目标用户信息失败：{e}。可能该用户不在本群中。"
            
            bot_role_cn = role_map.get(bot_role, bot_role)
            target_role_cn = role_map.get(target_role, target_role)

            # 3. 检查权限
            can_kick = False
            kick_fail_reason = ""
            
            if bot_role == 'owner':
                # 群主可以踢除管理员和成员，但不能踢自己
                if qq_id == bot_id:
                    kick_fail_reason = "不能踢出自己"
                elif target_role != 'owner':
                    can_kick = True
                else:
                    kick_fail_reason = "不能踢出群主（自己）"
            elif bot_role == 'admin':
                # 管理员只能踢除成员
                if qq_id == bot_id:
                    kick_fail_reason = "不能踢出自己"
                elif target_role == 'member':
                    can_kick = True
                elif target_role == 'admin':
                    kick_fail_reason = "管理员不能踢出其他管理员"
                elif target_role == 'owner':
                    kick_fail_reason = "管理员不能踢出群主"
            else:
                # 普通成员没有踢人权限
                kick_fail_reason = "机器人不是群管理员或群主，没有踢人权限"
            
            if not can_kick:
                return f"移出失败：{kick_fail_reason}。机器人身份：{bot_role_cn}，目标用户身份：{target_role_cn}"
            
            # 4. 执行踢人操作
            await call_onebot(
                client, 
                'set_group_kick', 
                group_id=group_id, 
                user_id=int(qq_id),
                reject_add_request=reject_add_request
            )
            
            reject_str = "，并拒绝其再次加群请求" if reject_add_request else ""
            return f"已将 {target_nickname} (QQ:{qq_id}) 移出本群{reject_str}。"

        except Exception as e:
            logger.error(f"Kick user failed: {e}")
            return f"移出群成员失败: {e}"