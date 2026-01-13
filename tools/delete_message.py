import time
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import delete_single_message, call_onebot, check_tool_permission, get_original_tool_name

class DeleteMessageTool(FunctionTool):
    def __init__(self, plugin):
        show_message_id = True
        if plugin:
            show_message_id = plugin.general_config.get("show_message_id", True)

        desc_message_id = "要撤回的消息ID。多个ID用逗号分隔，例如 '123,456'。可以直接从用户消息的 [MSG_ID:xxx] 中获取 xxx。"
        if not show_message_id:
            desc_message_id = "要撤回的消息ID。多个ID用逗号分隔，例如 '123,456'。如果你无法从当前上下文获取消息ID，请先使用 get_recent_messages 工具查找。"

        super().__init__(
            name="delete_message",
            description="撤回（删除）一条或多条指定 message_id 的消息。机器人只能撤回自己发送的消息（通常限制2分钟内），或在作为管理员时撤回群成员的消息。",
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

    async def _get_msg_info(self, client, message_id, session_id):
        # 1. Check cache
        cache = self.plugin.message_cache.get(session_id, [])
        for msg in cache:
            if str(msg.get("message_id")) == str(message_id):
                return msg

        # 2. Call API (兼容 message_id 为 "12345_6789" 等情况)
        try:
            mid_str = str(message_id).strip()
            if "[MSG_ID:" in mid_str:
                mid_str = mid_str.replace("[MSG_ID:", "").replace("]", "").strip()

            # NapCat 等实现可能用 12345_6789，优先取前半段
            mid_int = None
            try:
                mid_int = int(mid_str)
            except ValueError:
                if "_" in mid_str:
                    mid_int = int(mid_str.split("_", 1)[0])
                else:
                    raise

            resp = await call_onebot(client, 'get_msg', message_id=mid_int)
            if resp:
                sender = resp.get("sender", {}) if isinstance(resp, dict) else {}
                ts = None
                if isinstance(resp, dict):
                    ts = resp.get("time") or resp.get("timestamp")
                if ts is None:
                    ts = int(time.time())
                return {
                    "sender_id": str(sender.get("user_id")),
                    "timestamp": int(ts),
                    "message_id": str(message_id),
                }
        except Exception:
            pass
        return None

    async def _get_role(self, client, group_id, user_id):
        try:
            info = await call_onebot(client, 'get_group_member_info', group_id=int(group_id), user_id=int(user_id), no_cache=True)
            return info.get("role", "member")
        except Exception as e:
            logger.warning(f"Failed to get role for {user_id} in {group_id}: {e}")
            return "member"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        message_id = kwargs.get("message_id")
        
        if not isinstance(event, AiocqhttpMessageEvent):
             return "当前平台不支持撤回消息操作 (仅支持 OneBot/Aiocqhttp)。"
        
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

        client = event.bot
        session_id = event.get_session_id()
        self_id = str(event.get_self_id())
        
        # Parse IDs
        ids = [mid.strip() for mid in message_id.split(",") if mid.strip()]
        clean_ids = []
        for mid in ids:
            if "[MSG_ID:" in mid:
                mid = mid.replace("[MSG_ID:", "").replace("]", "")
            clean_ids.append(mid)
        ids = clean_ids

        results = []
        role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
        
        for mid in ids:
            # Check permissions
            msg_info = await self._get_msg_info(client, mid, session_id)
            if not msg_info:
                # 无法获取消息详情，尝试直接撤回
                try:
                    res = await delete_single_message(client, mid)
                    results.append(f"消息 {mid}: {res}")
                except Exception as e:
                    results.append(f"消息 {mid}: 撤回失败 (无法获取消息详情(可能已过期)且直接撤回失败: {e})")
                continue

            sender_id = str(msg_info["sender_id"])
            timestamp = msg_info["timestamp"]
            now = int(time.time())
            is_timeout = (now - timestamp) > 120
            
            # --- Private Chat ---
            if event.is_private_chat():
                if sender_id != self_id:
                    results.append(f"消息 {mid}: 撤回失败 (私聊不可撤回对方的消息)")
                    continue
                if is_timeout:
                    results.append(f"消息 {mid}: 撤回失败 (时间超过两分钟)")
                    continue
            
            # --- Group Chat ---
            else:
                group_id = event.get_group_id()
                # Get roles
                my_role = await self._get_role(client, group_id, self_id)
                target_role = await self._get_role(client, group_id, sender_id)
                
                my_role_cn = role_map.get(my_role, "成员")
                target_role_cn = role_map.get(target_role, "成员")

                # 1. Member
                if my_role == "member":
                    if sender_id != self_id:
                        results.append(f"消息 {mid}: 撤回失败 (权限不足，对方为{target_role_cn}，你为{my_role_cn})")
                        continue
                    if is_timeout:
                        results.append(f"消息 {mid}: 撤回失败 (时间超过两分钟，且你不是管理员)")
                        continue
                        
                # 2. Admin
                elif my_role == "admin":
                    if sender_id == self_id:
                        # 自己发的消息，通常管理员也可以撤回超时的
                        pass
                    elif target_role == "owner":
                        results.append(f"消息 {mid}: 撤回失败 (权限不足，对方为群主，你为管理员)")
                        continue
                    elif target_role == "admin":
                        if sender_id != self_id:
                             results.append(f"消息 {mid}: 撤回失败 (权限不足，对方为管理员，你为管理员)")
                             continue
                    else:
                        # Target is member. Admin can recall (ignore time).
                        pass
                
                # 3. Owner
                elif my_role == "owner":
                    # Can recall everyone.
                    pass

            # If passed checks, execute
            try:
                res = await delete_single_message(client, mid)
                results.append(f"消息 {mid}: {res}")
            except Exception as e:
                results.append(f"消息 {mid}: 撤回失败 ({e})")

        return "\n".join(results)