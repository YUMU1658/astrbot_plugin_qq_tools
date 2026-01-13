from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name


class GroupMuteAllTool(FunctionTool):
    def __init__(self, plugin=None):
        super().__init__(
            name="group_mute_all",
            description="查询或设置群全体禁言状态。需要机器人有管理员或群主权限。仅在群聊中可用。如果不传入参数，则查询当前群是否开启全体禁言。",
            parameters={
                "type": "object",
                "properties": {
                    "enable": {
                        "type": "string",
                        "description": "开启或关闭全体禁言。可选值：'开启'/'on'/'true'/'1' 表示开启，'关闭'/'off'/'false'/'0' 表示关闭。不传此参数则查询当前状态。",
                        "enum": ["开启", "关闭", "on", "off", "true", "false", "1", "0"]
                    }
                },
                "required": [],
            }
        )
        self.plugin = plugin

    def _parse_enable_param(self, enable_str: str) -> bool:
        """解析 enable 参数，返回 True 表示开启，False 表示关闭"""
        if enable_str is None:
            return None
        enable_lower = str(enable_str).lower().strip()
        if enable_lower in ["开启", "on", "true", "1"]:
            return True
        elif enable_lower in ["关闭", "off", "false", "0"]:
            return False
        return None

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
        
        enable_param = kwargs.get("enable")
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "仅在群聊中才可以使用全体禁言功能。"

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
            bot_role_cn = role_map.get(bot_role, bot_role)
            
            # 2. 检查是否有权限（需要是管理员或群主）
            if bot_role not in ['owner', 'admin']:
                return f"操作失败：机器人不是本群管理员或群主，无法操作全体禁言。当前身份：{bot_role_cn}"
            
            # 3. 如果没有传入 enable 参数，查询当前状态
            if enable_param is None:
                # 获取群信息，查看全体禁言状态
                group_info = await call_onebot(client, 'get_group_info', group_id=group_id, no_cache=True)
                
                # 不同的 OneBot 实现可能字段名不同
                # NapCat/go-cqhttp 通常使用 shutup_time_whole 或 whole_ban
                is_muted = False
                
                # 尝试多个可能的字段名
                if 'shutup_time_whole' in group_info:
                    # shutup_time_whole > 0 表示开启了全体禁言
                    is_muted = group_info.get('shutup_time_whole', 0) > 0
                elif 'whole_ban' in group_info:
                    is_muted = bool(group_info.get('whole_ban', False))
                elif 'all_muted' in group_info:
                    is_muted = bool(group_info.get('all_muted', False))
                else:
                    # 如果没有找到相关字段，尝试从原始返回中查找
                    logger.debug(f"Group info response: {group_info}")
                    # 最后的兜底：假设没有开启
                    return f"当前群{'已开启' if is_muted else '未开启'}全体禁言。(注：可能无法准确获取状态，请以实际群设置为准)"
                
                status_str = "已开启" if is_muted else "未开启"
                return f"当前群{status_str}全体禁言。"
            
            # 4. 解析 enable 参数
            enable = self._parse_enable_param(enable_param)
            if enable is None:
                return f"参数错误：无法解析 enable 参数 '{enable_param}'。请使用 '开启'/'关闭' 或 'on'/'off'。"
            
            # 5. 执行全体禁言设置
            await call_onebot(client, 'set_group_whole_ban', group_id=group_id, enable=enable)
            
            action_str = "开启" if enable else "关闭"
            return f"已成功{action_str}全体禁言。"

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Group mute all failed: {e}")
            
            # 解析常见错误
            if "SEND_MSG_API_ERROR" in error_msg or "retcode" in error_msg:
                return f"操作失败：API 调用错误，可能是权限不足或参数错误。详情：{error_msg}"
            elif "not found" in error_msg.lower():
                return f"操作失败：未找到群或 API 不支持。详情：{error_msg}"
            else:
                return f"操作失败：{error_msg}"