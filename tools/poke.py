import time
import asyncio
from typing import TYPE_CHECKING, Optional

from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api import logger
from ..utils import call_onebot

if TYPE_CHECKING:
    from ..main import QQToolsPlugin

class PokeTool(FunctionTool):
    def __init__(self, plugin: "QQToolsPlugin" = None):
        super().__init__(
            name="poke_user",
            description="发送“戳一戳”消息给指定QQ用户（双击头像效果）。返回结果包含动作文案（如“你踢了踢xxx”）。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标QQ号",
                    }
                },
                "required": ["qq_id"],
            }
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        qq_id = kwargs.get("qq_id")
        
        # 从配置读取是否返回戳一戳文案（默认启用）
        return_poke_info = True
        if self.plugin:
            general_config = self.plugin.config.get("general", {})
            return_poke_info = general_config.get("poke_return_info", True)
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持戳一戳 (仅支持 OneBot/Aiocqhttp)。"
        
        if not qq_id:
            return "未指定QQ号。"

        client = event.bot
        self_id = str(event.get_self_id())
        group_id = None
        is_group = event.message_obj.type == MessageType.GROUP_MESSAGE
        
        if is_group:
            group_id = str(event.message_obj.group_id)
        
        try:
            # 记录发送时间戳（用于匹配 poke notice）
            start_ts = time.time()
            
            if is_group:
                # 群聊戳一戳
                await call_onebot(client, "group_poke", group_id=int(group_id), user_id=int(qq_id))
            else:
                # 私聊戳一戳
                await call_onebot(client, "friend_poke", user_id=int(qq_id))
            
            # 如果不需要返回戳一戳信息，直接返回成功提示
            if not return_poke_info:
                return f"已戳一戳用户 {qq_id}。"
            
            # 需要返回戳一戳信息，等待并获取文案
            poke_text = await self._wait_for_poke_notice(
                client=client,
                self_id=self_id,
                target_id=qq_id,
                group_id=group_id,
                start_ts=start_ts,
                timeout=2.0
            )
            
            if poke_text:
                return poke_text
            else:
                # 降级：无法获取文案，返回默认格式
                target_name = await self._get_target_name(client, qq_id, group_id)
                return f"你戳了戳{target_name}"
                
        except Exception as e:
            logger.error(f"Poke failed: {e}")
            return f"戳一戳失败: {e}。可能NapCat不支持此API或参数错误。"

    async def _wait_for_poke_notice(
        self,
        client,
        self_id: str,
        target_id: str,
        group_id: Optional[str],
        start_ts: float,
        timeout: float = 2.0
    ) -> Optional[str]:
        """等待并获取 poke notice 事件，提取动作文案
        
        Args:
            client: OneBot 客户端
            self_id: 机器人自己的 QQ 号
            target_id: 被戳者的 QQ 号
            group_id: 群号（私聊时为 None）
            start_ts: poke 发送时间戳
            timeout: 等待超时时间（秒）
            
        Returns:
            动作文案字符串，如 "你踢了踢xxx"；如果获取失败返回 None
        """
        if not self.plugin:
            logger.warning("PokeTool: plugin instance not available, cannot get poke notice")
            return None
        
        # 获取目标用户显示名（用于拼装文案）
        target_name = await self._get_target_name(client, target_id, group_id)
        
        # 轮询查找匹配的 poke notice
        poll_interval = 0.1  # 100ms 轮询间隔
        elapsed = 0.0
        
        while elapsed < timeout:
            # 扫描 poke notice 缓存
            matched_notice = self._find_matching_poke_notice(
                self_id=self_id,
                target_id=target_id,
                group_id=group_id,
                start_ts=start_ts
            )
            
            if matched_notice:
                # 解析动作文案
                action_text = self._parse_poke_action(matched_notice)
                if action_text:
                    return f"你{action_text}{target_name}"
                else:
                    # 有事件但解析不到动作，使用默认
                    return f"你戳了戳{target_name}"
            
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        
        # 超时未找到匹配的 poke notice
        logger.debug(f"Poke notice not received within {timeout}s")
        return None

    def _find_matching_poke_notice(
        self,
        self_id: str,
        target_id: str,
        group_id: Optional[str],
        start_ts: float
    ) -> Optional[dict]:
        """在 poke notice 缓存中查找匹配的事件
        
        匹配条件：
        - user_id == self_id（机器人自己发起的 poke）
        - target_id == 传入的 target_id
        - group_id 匹配（群聊场景）
        - 事件时间 >= start_ts
        """
        if not self.plugin or not hasattr(self.plugin, 'poke_notice_cache'):
            return None
        
        for notice in self.plugin.poke_notice_cache:
            notice_time = notice.get('timestamp', 0)
            
            # 检查时间戳（必须在发送 poke 之后）
            if notice_time < start_ts:
                continue
            
            # 检查是否是机器人自己发起的 poke
            notice_user_id = str(notice.get('user_id', ''))
            if notice_user_id != self_id:
                continue
            
            # 检查目标是否匹配
            notice_target_id = str(notice.get('target_id', ''))
            if notice_target_id != target_id:
                continue
            
            # 群聊场景需要匹配 group_id
            if group_id:
                notice_group_id = str(notice.get('group_id', ''))
                if notice_group_id != group_id:
                    continue
            
            # 找到匹配的事件
            return notice
        
        return None

    def _parse_poke_action(self, notice: dict) -> Optional[str]:
        """从 poke notice 中解析动作文本
        
        NapCat 的 poke 事件可能包含 raw_info 字段，格式如：
        [
            {"type": "nor", "txt": "拍了拍"},
            {"type": "nor", "txt": "的希望肉罐头..."}
        ]
        
        或者旧版可能有 raw_message 字段
        
        Returns:
            动作文本（如 "踢了踢"、"拍了拍"），不包含目标名字；
            如果解析失败返回 None
        """
        # 优先尝试 raw_info
        raw_info = notice.get('raw_info')
        if raw_info:
            action = self._extract_action_from_raw_info(raw_info)
            if action:
                return action
        
        # 兼容：尝试 raw_message
        raw_message = notice.get('raw_message')
        if raw_message:
            action = self._extract_action_from_raw_message(raw_message)
            if action:
                return action
        
        # 兼容：尝试从事件的 raw_event 中获取
        raw_event = notice.get('raw_event', {})
        if isinstance(raw_event, dict):
            raw_info = raw_event.get('raw_info')
            if raw_info:
                action = self._extract_action_from_raw_info(raw_info)
                if action:
                    return action
            
            raw_message = raw_event.get('raw_message')
            if raw_message:
                action = self._extract_action_from_raw_message(raw_message)
                if action:
                    return action
        
        return None

    def _extract_action_from_raw_info(self, raw_info) -> Optional[str]:
        """从 raw_info 提取动作文本
        
        raw_info 格式：
        - list: [{"type": "nor", "txt": "拍了拍"}, {"type": "nor", "txt": "的..."}]
        - 或其他格式
        """
        if isinstance(raw_info, list):
            # 遍历段落，收集 type 为 "nor" 的 txt
            action_parts = []
            for item in raw_info:
                if isinstance(item, dict) and item.get('type') == 'nor':
                    txt = item.get('txt', '')
                    if txt:
                        action_parts.append(txt)
            
            # 第一个通常是动作（如"踢了踢"、"拍了拍"）
            if action_parts:
                return action_parts[0]
        
        elif isinstance(raw_info, str):
            # 如果是字符串，尝试直接提取动作
            # 常见格式："戳了戳"、"拍了拍xxx"等
            import re
            match = re.match(r'^([踢拍戳捏揉亲]了[踢拍戳捏揉亲])', raw_info)
            if match:
                return match.group(1)
            return raw_info[:6] if raw_info else None  # 截取前几个字符
        
        return None

    def _extract_action_from_raw_message(self, raw_message) -> Optional[str]:
        """从 raw_message 提取动作文本（兼容旧实现）"""
        import re
        
        if isinstance(raw_message, list):
            # 类似 raw_info 的列表格式
            return self._extract_action_from_raw_info(raw_message)
        
        elif isinstance(raw_message, str):
            # 字符串格式，尝试提取动作
            match = re.match(r'^([踢拍戳捏揉亲]了[踢拍戳捏揉亲])', raw_message)
            if match:
                return match.group(1)
            return None
        
        return None

    async def _get_target_name(self, client, target_id: str, group_id: Optional[str]) -> str:
        """获取目标用户的显示名称
        
        群聊：优先使用群名片，其次昵称
        私聊：使用昵称
        
        获取失败时使用 QQ 号作为兜底
        """
        try:
            if group_id:
                # 群聊：获取群成员信息
                info = await call_onebot(
                    client,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(target_id),
                    no_cache=True
                )
                if info:
                    name = info.get('card', '') or info.get('nickname', '')
                    if name:
                        return name
            else:
                # 私聊：获取陌生人/好友信息
                try:
                    info = await call_onebot(
                        client,
                        "get_stranger_info",
                        user_id=int(target_id),
                        no_cache=True
                    )
                    if info:
                        name = info.get('nickname', '') or info.get('nick', '')
                        if name:
                            return name
                except Exception:
                    pass
                
                # 尝试好友信息
                try:
                    info = await call_onebot(
                        client,
                        "get_friend_info",
                        user_id=int(target_id)
                    )
                    if info:
                        name = info.get('nickname', '') or info.get('remark', '')
                        if name:
                            return name
                except Exception:
                    pass
        
        except Exception as e:
            logger.debug(f"Failed to get target name for {target_id}: {e}")
        
        # 兜底：使用 QQ 号
        return target_id