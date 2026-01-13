from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from ..utils import call_onebot

class RepeatMessageTool(FunctionTool):
    def __init__(self, plugin=None):
        show_message_id = True
        if plugin:
            show_message_id = plugin.general_config.get("show_message_id", True)

        desc_message_id = "要复读的消息ID。"
        if not show_message_id:
            desc_message_id = "要复读的消息ID。如果你无法从当前上下文获取消息ID，请先使用 get_recent_messages 工具查找。"

        super().__init__(
            name="repeat_message",
            description="复读（原样转发）指定 message_id 的消息到当前会话。",
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

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        if not isinstance(event, AiocqhttpMessageEvent):
            return "此工具仅支持 QQ 平台 (Napcat/OneBot 11)。"
            
        message_id = kwargs.get("message_id")
        
        # Clean message_id
        if message_id and "[MSG_ID:" in message_id:
            message_id = message_id.replace("[MSG_ID:", "").replace("]", "")
            
        if not message_id:
            return "消息ID为空。"

        client = event.bot
        
        try:
            if event.get_group_id():
                # Group chat
                # Napcat/go-cqhttp extension for forwarding single message to group
                await call_onebot(
                    client,
                    "forward_group_single_msg",
                    group_id=int(event.get_group_id()),
                    message_id=message_id
                )
            else:
                # Private chat
                # Napcat/go-cqhttp extension for forwarding single message to friend
                await call_onebot(
                    client,
                    "forward_friend_single_msg",
                    user_id=int(event.get_sender_id()),
                    message_id=message_id
                )
            return f"已复读消息 {message_id}"
        except Exception as e:
            return f"复读失败: {e}。请确认使用的是 Napcat 且支持 forward_group_single_msg/forward_friend_single_msg 接口。"