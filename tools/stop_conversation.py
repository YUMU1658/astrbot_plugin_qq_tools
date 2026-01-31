import json
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.provider.entities import ProviderRequest

class StopConversationTool(FunctionTool):
    def __init__(self):
        super().__init__(
            name="stop_conversation",
            description="立即结束当前对话轮次。当你认为任务已完成、无需进一步回复，或已通过其他方式发送了响应时，调用此工具以停止输出。",
            parameters={
                "type": "object",
                "properties": {},
                "required": []
            }
        )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        
        # 手动保存对话记录，因为直接返回 None 会导致 Agent Loop 结束，
        # 而 AstrBot 的默认逻辑可能无法正确保存最后一次 tool call 导致的结束状态。
        req = event.get_extra("provider_request")
        if isinstance(req, ProviderRequest) and req.conversation:
            try:
                # 获取 ConversationManager
                # context.context -> AstrAgentContext
                # AstrAgentContext.context -> Context (Plugin Context)
                conv_manager = context.context.context.conversation_manager
                
                # 重新解析完整的历史记录 (req.contexts 可能是被截断的)
                # 使用 req.conversation.history 确保我们不会丢失早期的上下文
                messages = json.loads(req.conversation.history) if req.conversation.history else []
                
                # 追加当前用户的消息
                messages.append(await req.assemble_context())
                
                # 追加助手停止对话的消息
                messages.append({
                    "role": "assistant",
                    "content": "Conversation stopped."
                })
                
                # 更新数据库
                await conv_manager.update_conversation(
                    event.unified_msg_origin,
                    req.conversation.cid,
                    history=messages
                )
            except Exception as e:
                # 记录错误但不中断流程
                from astrbot.core import logger
                logger.error(f"Failed to save conversation history in stop_conversation: {e}")

        # 返回 None，触发 Agent Runner 结束任务 (Transition to DONE state)
        # 不调用 event.stop_event()，以免破坏 pipeline 的正常收尾
        return None