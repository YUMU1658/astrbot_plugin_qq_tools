import time
from datetime import datetime
from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class GetRecentMessagesTool(FunctionTool):
    def __init__(self, plugin):
        show_message_id = True
        if plugin:
            show_message_id = plugin.general_config.get("show_message_id", True)

        desc = "搜索当前会话的历史消息记录。当需要查找上下文之外的消息（如之前的对话、特定关键词、特定发送者）以获取 message_id 或回顾内容时使用。"
        if not show_message_id:
            desc = "搜索当前会话的历史消息记录。当需要查找上下文之外的消息（如之前的对话、特定关键词、特定发送者）以获取 message_id (如果需要引用/撤回/转发) 或回顾内容时使用。"

        super().__init__(
            name="get_recent_messages",
            description=desc,
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "获取的消息数量，默认为 20。",
                        "default": 20
                    },
                    "sender_filter": {
                        "type": "string",
                        "description": "可选，按发送者昵称过滤（支持模糊匹配）。"
                    },
                    "sender_id": {
                        "type": "string",
                        "description": "可选，按发送者 QQ 号精确过滤。使用 'bot' 或 'self' 表示 BOT 自己的消息。"
                    },
                    "keyword": {
                        "type": "string",
                        "description": "可选，按消息内容关键词过滤。"
                    },
                    "include_bot_messages": {
                        "type": "boolean",
                        "description": "是否包含自己发送的消息，默认为 true。",
                        "default": True
                    },
                    "start_time": {
                        "type": "string",
                        "description": "可选，起始时间，格式 'YYYY-MM-DD HH:MM:SS'。"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "可选，结束时间，格式 'YYYY-MM-DD HH:MM:SS'。"
                    }
                },
                "required": []
            }
        )
        self.plugin = plugin

    def _merge_messages(self, cached: list, api: list) -> list:
        """合并缓存消息和 API 消息，去重
        
        Args:
            cached: 缓存中的消息列表
            api: 从 API 获取的消息列表
            
        Returns:
            合并去重后的消息列表，按时间戳倒序排列
        """
        seen_ids = set()
        merged = []
        
        # 优先使用缓存消息（可能有更多处理过的信息）
        for msg in cached:
            msg_id = str(msg.get('message_id', ''))
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                merged.append(msg)
        
        # 添加 API 消息中缓存没有的
        for msg in api:
            msg_id = str(msg.get('message_id', ''))
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                merged.append(msg)
        
        # 按时间戳倒序排序（最新的在前面）
        merged.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        
        return merged

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        count = kwargs.get("count", 20)
        sender_filter = kwargs.get("sender_filter")
        sender_id_filter = kwargs.get("sender_id")
        keyword = kwargs.get("keyword")
        include_bot_messages = kwargs.get("include_bot_messages", True)
        start_time = kwargs.get("start_time")
        end_time = kwargs.get("end_time")

        session_id = event.get_session_id()
        self_id = str(event.get_self_id())
        
        # 处理 sender_id 参数：支持 'bot' 或 'self' 表示 BOT 自己
        if sender_id_filter and sender_id_filter.lower() in ['bot', 'self']:
            sender_id_filter = self_id

        # 1. 从缓存获取消息（使用 _get_session_cache 确保更新活跃时间）
        cached_messages = list(self.plugin._get_session_cache(session_id))
        
        # 2. 从 API 获取历史消息（确保包含 BOT 消息）
        api_messages = []
        if include_bot_messages and isinstance(event, AiocqhttpMessageEvent):
            try:
                api_messages = await self.plugin.fetch_history_from_api(event, count * 2)
            except Exception as e:
                logger.warning(f"Failed to fetch history from API: {e}")
        
        # 3. 合并去重
        messages = self._merge_messages(cached_messages, api_messages)
        
        if not messages:
            return "当前会话没有消息记录。"
        
        # 解析时间
        start_ts = 0
        end_ts = float('inf')
        if start_time:
            try:
                start_ts = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return "start_time 格式错误，请使用 YYYY-MM-DD HH:MM:SS"
        if end_time:
            try:
                end_ts = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return "end_time 格式错误，请使用 YYYY-MM-DD HH:MM:SS"

        matched_msgs = []
        
        logger.info(f"Searching messages in session {session_id}. Total: {len(messages)} (cached: {len(cached_messages)}, api: {len(api_messages)}). Filter: sender={sender_filter}, sender_id={sender_id_filter}, keyword={keyword}, time={start_time}-{end_time}")

        for msg in messages:
            if len(matched_msgs) >= count:
                break
                
            # 过滤逻辑
            # 时间过滤
            if not (start_ts <= msg["timestamp"] <= end_ts):
                continue

            # 发送者昵称过滤（模糊匹配）
            if sender_filter:
                # 忽略大小写，且只要包含即可
                if sender_filter.lower() not in msg["sender_name"].lower():
                    continue
            
            # 发送者 ID 精确过滤
            if sender_id_filter:
                if str(msg.get("sender_id", "")) != str(sender_id_filter):
                    continue
            
            # 关键词过滤
            if keyword:
                if keyword not in msg["content"]:
                    continue
            
            matched_msgs.append(msg)

        if not matched_msgs:
            logger.info("No messages found matching criteria.")
            # 如果有过滤条件但没找到，尝试返回最近的几条消息作为参考
            if sender_filter or sender_id_filter or keyword or start_time or end_time:
                fallback_result = []
                for msg in messages[:5]:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg["timestamp"]))
                    sender_qq = msg.get('sender_id', 'Unknown')
                    is_bot = " [BOT]" if str(sender_qq) == self_id else ""
                    fallback_result.append(f"[{time_str}] {msg['sender_name']}{is_bot}({sender_qq}) (ID: {msg['message_id']}): {msg['content'][:50]}")
                
                hint = ""
                if sender_id_filter == self_id:
                    hint = "\n提示：您正在查找 BOT 发送的消息。BOT 消息的 sender_id 是 " + self_id
                
                return f"未找到完全匹配的消息。以下是最近的 5 条消息，请检查筛选条件是否正确：{hint}\n" + "\n".join(fallback_result)
            
            return "未找到符合条件的消息。"

        # 格式化输出
        result = []
        use_detail_preview = len(matched_msgs) <= 50
        
        for msg in matched_msgs:
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg["timestamp"]))
            
            if use_detail_preview:
                # 详细预览模式
                # 格式: [Time] Sender(QQ) (ID: ID): Preview
                content = msg['content']
                preview = content
                if len(content) > 50:
                    preview = f"{content[:25]}...{content[-20:]}"
                
                sender_qq = msg.get('sender_id', 'Unknown')
                # 标记 BOT 消息
                is_bot_msg = msg.get('is_bot_message', False) or str(sender_qq) == self_id
                bot_marker = " [BOT]" if is_bot_msg else ""
                
                result.append(f"[{time_str}] {msg['sender_name']}{bot_marker}({sender_qq}) (ID: {msg['message_id']}): {preview}")
            else:
                # 原有模式 (数量>50)
                sender_qq = msg.get('sender_id', 'Unknown')
                is_bot_msg = msg.get('is_bot_message', False) or str(sender_qq) == self_id
                bot_marker = " [BOT]" if is_bot_msg else ""
                result.append(f"[{time_str}] {msg['sender_name']}{bot_marker} (ID: {msg['message_id']}): {msg['content']}")
        
        # 添加统计信息
        bot_count = sum(1 for m in matched_msgs if m.get('is_bot_message', False) or str(m.get('sender_id', '')) == self_id)
        stats = f"\n\n统计：共 {len(matched_msgs)} 条消息"
        if bot_count > 0:
            stats += f"，其中 BOT 发送 {bot_count} 条"
            
        return "最近的消息列表:\n" + "\n".join(result) + stats