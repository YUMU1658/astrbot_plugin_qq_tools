from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.agent.message import ImageURLPart
from astrbot.core.provider.provider import Provider
from ..utils import call_onebot


class ViewAvatarTool(FunctionTool):
    def __init__(self, plugin_instance):
        super().__init__(
            name="view_qq_avatar",
            description="获取并查看指定QQ用户的头像图片。调用后图片将进入你的视觉上下文，你可以对其进行描述或评价。注意：此工具仅用于让你“看”图片，不会将图片发送给用户。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标QQ号。如果不填，默认查看BOT自己的头像。如需查看消息发送者的头像，请先获取其QQ号再调用。",
                    },
                },
                "required": [],
            }
        )
        self.plugin = plugin_instance
        self.config = self.plugin.config.get("view_avatar_config", {})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        qq_id = kwargs.get("qq_id")
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        client = event.bot
        
        # 1. 解析 QQ 号
        user_id = qq_id
        
        # 如果没有指定 QQ 号，获取 BOT 自己的 QQ 号
        if not user_id:
            try:
                login_info = await call_onebot(client, 'get_login_info')
                user_id = str(login_info.get('user_id', ''))
            except Exception as e:
                logger.error(f"Failed to get bot login info: {e}")
                return f"获取BOT信息失败: {e}"
        
        # 提取纯数字
        user_id = "".join([c for c in str(user_id) if c.isdigit()])
        if not user_id:
            return "❌ 获取失败：无法识别有效的QQ号。"

        # 2. 构造高清头像 URL (腾讯官方接口，s=640为高清)
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        # 3. 根据配置选择查看方式
        view_mode = self.config.get("view_mode", "context")
        
        if view_mode == "describe":
            # 使用指定的模型描述图片
            return await self._describe_avatar(context, user_id, avatar_url)
        else:
            # 默认：插入上下文让 LLM 直接看图
            return await self._inject_to_context(context, user_id, avatar_url)

    async def _inject_to_context(self, context: ContextWrapper[AstrAgentContext], user_id: str, avatar_url: str) -> str:
        """将头像图片注入到 LLM 上下文中"""
        try:
            # 获取会话历史 (context.messages 是 ContextWrapper 的 messages 字段)
            messages = context.messages
            
            if messages:
                # 构造一个图片组件
                img_part = ImageURLPart(
                    image_url=ImageURLPart.ImageURL(
                        url=avatar_url,
                        id=f"avatar_{user_id}"
                    )
                )
                
                # 查找最近的一条 User 消息，将图片追加进去
                found_user_msg = False
                for msg in reversed(messages):
                    if msg.role == "user":
                        # 确保 content 是列表以便追加
                        if isinstance(msg.content, str):
                            from astrbot.core.agent.message import TextPart
                            msg.content = [TextPart(text=msg.content)]
                        
                        # 追加图片
                        if isinstance(msg.content, list):
                            msg.content.append(img_part)
                            found_user_msg = True
                            logger.info(f"已将头像 {user_id} 注入到 LLM 上下文中。")
                            break
                
                if not found_user_msg:
                    logger.warning("未找到用户消息，无法注入图片。将返回图片链接。")
                    return (
                        f"获取成功。图片链接：![avatar]({avatar_url})\n"
                        f"(注入失败，请尝试直接读取链接)"
                    )

        except Exception as e:
            logger.error(f"视觉注入失败: {e}")
            # 如果注入失败，回退到 Markdown 图片链接 (部分强力模型如 GPT-4o 也能识别)
            return (
                f"获取成功。图片链接：![avatar]({avatar_url})\n"
                f"(注入失败，请尝试直接读取链接)"
            )

        # 返回工具执行结果
        return (
            f"已成功获取用户 {user_id} 的头像数据。\n"
            f"系统提示：该图片已成功加载到你的视觉上下文中（作为用户消息的一部分）。\n"
            f"请忽略这是一个链接的事实，直接利用你的视觉能力(Vision)描述你看到的图片内容。\n"
            f"不要向用户展示 URL。"
        )

    async def _describe_avatar(self, context: ContextWrapper[AstrAgentContext], user_id: str, avatar_url: str) -> str:
        """使用指定的模型描述头像图片"""
        provider_id = self.config.get("describe_provider_id", "")
        prompt = self.config.get("describe_prompt", "请详细描述这个头像图片的内容，包括人物特征、风格、颜色等。")
        
        if not provider_id:
            # 如果没有配置 provider_id，回退到上下文注入方式
            logger.warning("未配置 describe_provider_id，回退到上下文注入方式")
            return await self._inject_to_context(context, user_id, avatar_url)
        
        try:
            # 获取 Context 对象
            astrbot_context = context.context.context
            
            # 获取指定的 Provider
            provider = astrbot_context.get_provider_by_id(provider_id)
            
            if not provider:
                return f"❌ 配置错误：未找到 ID 为 {provider_id} 的模型服务商。\n💡 提示: 请检查 view_avatar_config.describe_provider_id 配置是否正确。"
            
            if not isinstance(provider, Provider):
                return f"❌ 配置错误：{provider_id} 不是一个文本生成模型。\n💡 提示: 请配置一个支持图片输入的 Chat Completion 类型模型。"
            
            logger.info(f"使用 {provider_id} 描述头像: {avatar_url}")
            
            # 调用 LLM 进行图像描述
            llm_response = await provider.text_chat(
                prompt=prompt,
                image_urls=[avatar_url],
            )
            
            if llm_response and llm_response.completion_text:
                description = llm_response.completion_text
                return (
                    f"✅ 已成功获取并分析用户 {user_id} 的头像。\n\n"
                    f"【头像描述】\n{description}"
                )
            else:
                return f"❌ 模型未返回描述内容。\n💡 提示: 请检查模型是否支持图片输入。"
                
        except Exception as e:
            logger.error(f"描述头像失败: {e}")
            error_msg = str(e)
            
            # 提供有针对性的错误提示
            if "image" in error_msg.lower() or "vision" in error_msg.lower():
                return (
                    f"❌ 图像描述失败\n"
                    f"🔴 错误信息: {error_msg}\n"
                    f"💡 提示: 配置的模型可能不支持图片输入，请选择支持视觉能力的模型（如 GPT-4o、Claude 3、Gemini 等）。"
                )
            else:
                return (
                    f"❌ 图像描述失败\n"
                    f"🔴 错误信息: {error_msg}\n"
                    f"💡 提示: 请检查模型配置和网络连接。"
                )