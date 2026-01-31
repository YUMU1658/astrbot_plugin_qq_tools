from astrbot.api import message_components as Comp
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
import re
from ..utils import parse_at_content

class ReplyMessageTool(FunctionTool):
    def __init__(self, plugin):
        show_message_id = True
        if plugin:
            show_message_id = plugin.general_config.get("show_message_id", True)

        desc_message_id = "要回复的目标消息ID。可以直接从用户消息的 [MSG_ID:xxx] 中获取 xxx。"
        if not show_message_id:
            desc_message_id = "要回复的目标消息ID。如果你无法从当前上下文获取消息ID，请先使用 get_recent_messages 工具查找。"

        super().__init__(
            name="reply_message",
            description="对指定 message_id 的消息进行引用回复。警告：此工具会直接发送消息给用户。调用成功后，严禁复述发送的内容。",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": desc_message_id
                    },
                    "content": {
                        "type": "string",
                        "description": "回复的内容。"
                    }
                },
                "required": ["message_id", "content"]
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        message_id = kwargs.get("message_id")
        content = kwargs.get("content")

        # 处理 message_id，去除可能的 [MSG_ID:xxx] 包装
        if message_id and "[MSG_ID:" in message_id:
            message_id = message_id.replace("[MSG_ID:", "").replace("]", "")

        # 过滤内容 (Regex)
        filter_patterns = self.plugin.compatibility_config.get("filter_patterns", ["&&.*?&&"])
        if filter_patterns:
            for pattern in filter_patterns:
                try:
                    content = re.sub(pattern, "", content)
                except Exception as e:
                    # 如果 regex 错误，忽略
                    pass
            content = content.strip()

        if not content:
            return "回复内容为空（已被过滤或原始为空），未发送。"

        # 构造回复链
        chain = [Comp.Reply(id=message_id)] # 引用消息
        
        # 处理内容中的 At
        if self.plugin.general_config.get("enable_auto_at_conversion", False):
            chain.extend(parse_at_content(content))
        else:
            chain.append(Comp.Plain(content))
        
        # 发送消息
        await event.send(event.chain_result(chain))
        return "[System: Reply sent successfully. Execution finished. STOP generating text.]"