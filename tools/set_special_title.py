from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name


def get_qq_title_display_length(text: str) -> str:
    """计算 QQ 头衔的显示字数描述
    
    QQ 头衔的字数限制比较特殊：
    - 中文字符算 1 个字
    - 英文字母/数字可能超过 6 个（实际以 QQ 计算为准）
    - emoji 可能算 1.5 个字等
    
    这里返回一个描述性的字符串，而不是硬性的数字计算。
    """
    # 简单统计：
    # - ASCII 字符（英文、数字、符号）
    # - 非 ASCII 字符（中文、emoji 等）
    ascii_count = 0
    non_ascii_count = 0
    
    for char in text:
        if ord(char) < 128:
            ascii_count += 1
        else:
            non_ascii_count += 1
    
    # 返回描述性文本
    # 实际字数限制以 QQ 为准，这里只是给用户一个参考
    total_chars = len(text)
    return f"{total_chars}字符"


class SetSpecialTitleTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="set_special_title",
            description="设置指定群成员的专属头衔（群头衔）。仅群主可以设置头衔，管理员无法设置。头衔有6字左右的限制（实际以QQ计算为准）。可以设置新头衔、清除头衔或恢复为默认等级头衔。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标用户的QQ号（必填）",
                    },
                    "title": {
                        "type": "string",
                        "description": "要设置的专属头衔内容。头衔有6字左右的限制。如果设置 restore_default=true，此参数会被忽略。",
                    },
                    "restore_default": {
                        "type": "boolean",
                        "description": "是否恢复为默认等级头衔。设为 true 时将清除专属头衔，让用户使用群等级系统自动分配的头衔。默认为 false。",
                        "default": False
                    },
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
        new_title = kwargs.get("title", "")
        restore_default = kwargs.get("restore_default", False)
        
        # 如果是恢复默认头衔，将 title 设为空
        if restore_default:
            new_title = ""
        
        if not qq_id:
            return "错误：必须提供目标用户的QQ号 (qq_id)。"
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "仅在群聊中才可以设置头衔。"

        group_id = event.message_obj.group_id
        client = event.bot
        
        # 角色名称映射
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
            bot_role_cn = role_map.get(bot_role, bot_role)
            
            # 2. 检查机器人是否是群主（只有群主才能设置头衔）
            if bot_role != 'owner':
                return f"设置头衔失败：权限不足。只有群主才能设置专属头衔，当前机器人身份为「{bot_role_cn}」。"
            
            # 3. 获取目标用户信息
            try:
                target_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(qq_id), no_cache=True)
                target_nickname = target_member_info.get('card') or target_member_info.get('nickname') or str(qq_id)
                old_title = target_member_info.get('title', '') or ''
            except Exception as e:
                return f"获取目标用户信息失败：{e}。可能该用户不在本群中。"
            
            # 4. 执行设置头衔操作
            # OneBot API: set_group_special_title
            # 参数：group_id, user_id, special_title, duration (可选，-1为永久)
            try:
                await call_onebot(
                    client, 
                    'set_group_special_title', 
                    group_id=group_id, 
                    user_id=int(qq_id),
                    special_title=new_title,
                    duration=-1  # 永久
                )
            except Exception as e:
                error_msg = str(e).lower()
                # 尝试分析错误原因
                if 'permission' in error_msg or '权限' in error_msg:
                    return f"设置头衔失败：权限不足。{e}"
                elif 'not found' in error_msg or '不存在' in error_msg:
                    return f"设置头衔失败：目标用户不在群中。{e}"
                elif 'too long' in error_msg or '过长' in error_msg or 'length' in error_msg:
                    return f"设置头衔失败：头衔内容过长，请缩短后重试。{e}"
                else:
                    return f"设置头衔失败：{e}"
            
            # 5. 构建成功返回消息
            title_len_desc = get_qq_title_display_length(new_title)
            
            if new_title:
                if old_title:
                    return f'已成功修改{target_nickname}的头衔，从"{old_title}"修改为"{new_title}"，{title_len_desc}/6字'
                else:
                    return f'已成功为{target_nickname}设置头衔"{new_title}"，{title_len_desc}/6字'
            else:
                # 清除头衔或恢复默认
                if restore_default:
                    if old_title:
                        return f'已成功将{target_nickname}的头衔恢复为默认（原专属头衔："{old_title}"已清除，现在将显示群等级对应的头衔）'
                    else:
                        return f"操作完成：{target_nickname}原本没有专属头衔，已确认使用默认等级头衔。"
                else:
                    if old_title:
                        return f'已成功清除{target_nickname}的头衔（原头衔："{old_title}"）'
                    else:
                        return f"操作完成：{target_nickname}原本没有头衔，现在也没有头衔。"

        except Exception as e:
            logger.error(f"Set special title failed: {e}")
            return f"设置头衔失败: {e}"