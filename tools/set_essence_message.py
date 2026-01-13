from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot, check_tool_permission, get_original_tool_name

class SetEssenceMessageTool(FunctionTool):
    def __init__(self, plugin=None):
        show_message_id = True
        if plugin:
            show_message_id = plugin.general_config.get("show_message_id", True)

        desc_message_id = "要设为精华的消息ID。多个ID用逗号分隔，例如 '123,456'。可以直接从用户消息的 [MSG_ID:xxx] 中获取 xxx。"
        if not show_message_id:
            desc_message_id = "要设为精华的消息ID。多个ID用逗号分隔。如果你无法从当前上下文获取消息ID，请先使用 get_recent_messages 工具查找。"

        super().__init__(
            name="set_essence_message",
            description="将指定 message_id 的消息设置为群精华消息。仅限群聊，需管理员权限。",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": desc_message_id
                    }
                },
                "required": ["message_id"]
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        message_id = kwargs.get("message_id")
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        if not hasattr(event.message_obj, "group_id") or not event.message_obj.group_id:
            return "设置精华消息失败：仅在群聊中支持此功能。"
        
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
        
        # 处理消息ID列表
        ids = [mid.strip() for mid in message_id.split(",") if mid.strip()]
        clean_ids = []
        for mid in ids:
            if "[MSG_ID:" in mid:
                mid = mid.replace("[MSG_ID:", "").replace("]", "")
            clean_ids.append(mid)
        ids = clean_ids

        if not ids:
            return "未提供有效的消息ID。"

        # 检查机器人权限
        try:
            login_info = await call_onebot(client, 'get_login_info')
            bot_id = str(login_info.get('user_id'))
            bot_member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(bot_id), no_cache=True)
            bot_role = bot_member_info.get('role', 'member')
            
            if bot_role == 'member':
                return "设置精华消息失败：机器人权限不足。请将机器人设置为管理员或群主。"
        except Exception as e:
            logger.warning(f"Failed to check bot role: {e}")
            # 如果检查失败，尝试继续执行，依靠API返回错误

        results = []
        success_count = 0
        
        for mid in ids:
            try:
                # 尝试转为int，NapCat/OneBot通常需要int类型的message_id
                try:
                    real_id = int(mid)
                except ValueError:
                    # 尝试处理带下划线的ID (e.g. go-cqhttp style)
                    if "_" in mid:
                         real_id = int(mid.split("_")[0])
                    else:
                        results.append(f"消息 {mid}: ID格式错误")
                        continue

                await call_onebot(client, 'set_essence_msg', message_id=real_id)
                results.append(f"消息 {mid}: 设置成功")
                success_count += 1
                
            except Exception as e:
                error_msg = str(e)
                reason = "未知错误"
                
                # 尝试解析常见错误
                if "100" in error_msg: # 这是一个假设的错误码，实际需视实现而定
                    reason = "可能是精华消息数量已达上限"
                elif "limit" in error_msg.lower():
                    reason = "精华消息数量已达上限"
                elif "permission" in error_msg.lower() or "403" in error_msg:
                    reason = "权限不足"
                elif "not found" in error_msg.lower():
                    reason = "消息不存在或已撤回"
                else:
                    reason = f"API调用失败 ({error_msg})"
                
                results.append(f"消息 {mid}: 设置失败 ({reason})")

        return "\n".join(results)