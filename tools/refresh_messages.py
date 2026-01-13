import time
import asyncio
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

class RefreshMessagesTool(FunctionTool):
    def __init__(self, plugin):
        super().__init__(
            name="refresh_messages",
            description="等待并获取当前会话中最新收到的消息。适用于你觉得对方话没说完，或者需要等待对方进一步回复的场景。",
            parameters={
                "type": "object",
                "properties": {
                    "duration": {
                        "type": "integer",
                        "description": "休眠时间，单位秒，默认 8 秒。",
                        "default": 8
                    }
                },
                "required": []
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        duration = kwargs.get("duration", 8)

        session_id = event.get_session_id()
        if not session_id:
            return "无法获取当前会话ID。"
            
        # 记录开始等待的时间
        start_time = int(time.time())
        
        # 休眠指定时间
        await asyncio.sleep(duration)
        
        # 获取缓存中的消息（使用 _get_session_cache 确保会话存在并更新活跃时间）
        cache = self.plugin._get_session_cache(session_id)
        if not cache:
            return "暂无新消息。"
            
        new_messages = []
        current_msg_id = event.message_obj.message_id
        
        messages = list(cache)
        
        for msg in messages:
            # 筛选在开始等待之后（或同时）收到的消息
            if msg["timestamp"] >= start_time:
                # 排除触发当前对话的那条消息
                if str(msg["message_id"]) == str(current_msg_id):
                    continue
                new_messages.append(msg)
        
        if not new_messages:
            return "暂无新消息。"
            
        result = []
        for msg in new_messages:
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg["timestamp"]))
            sender_qq = msg.get('sender_id', 'Unknown')
            result.append(f"[{time_str}] {msg['sender_name']}({sender_qq}): {msg['content']}")
            
        return "新消息列表:\n" + "\n".join(result)