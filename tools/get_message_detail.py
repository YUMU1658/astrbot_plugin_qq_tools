import json
import base64
import asyncio
import aiohttp
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, Tuple
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.agent.message import ImageURLPart
from ..utils import call_onebot

# Gemini 支持的图片格式
SUPPORTED_IMAGE_FORMATS = {'image/png', 'image/jpeg', 'image/webp'}
# 需要转换的格式
CONVERT_IMAGE_FORMATS = {'image/gif', 'image/bmp', 'image/tiff', 'image/ico'}

# 图片转换专用线程池（限制并发数，避免占用过多资源）
_image_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='img_conv')


class GetMessageDetailTool(FunctionTool):
    """获取消息详情工具
    
    根据 message_id 获取完整的消息结构，包括：
    - 原始消息段 (message segments)
    - 解析后的摘要
    - 文件信息 (文件 id / url / size)
    - 回复链信息
    - 可选：自动将图片注入上下文
    """
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="get_message_detail",
            description="根据消息ID获取完整的消息结构和详细信息。用于查看消息是否包含图片/文件/转发、获取文件ID和URL、查看回复链等。可选将图片注入视觉上下文。",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "要查询的消息ID (MSG_ID)",
                    },
                    "inject_images": {
                        "type": "boolean",
                        "description": "是否将消息中的图片注入到视觉上下文中（让你能「看到」图片）。默认跟随配置。",
                    },
                    "fetch_reply_chain": {
                        "type": "boolean",
                        "description": "是否递归获取引用消息的详情（回复链）。默认 true，最多追溯 3 层。",
                        "default": True
                    }
                },
                "required": ["message_id"],
            }
        )
        self.plugin = plugin_instance
        self.config = self.plugin.config.get("message_detail_config", {})
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        message_id = kwargs.get("message_id")
        inject_images = kwargs.get("inject_images")
        fetch_reply_chain = kwargs.get("fetch_reply_chain", True)
        
        if not message_id:
            return "❌ 缺少参数：请提供 message_id"
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "❌ 当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)"
        
        client = event.bot
        
        # 如果未指定 inject_images，使用配置的默认值
        if inject_images is None:
            inject_images = self.config.get("auto_inject_images", False)
        
        max_reply_depth = self.config.get("max_reply_chain_depth", 3)
        
        try:
            # 获取消息详情
            result = await self._get_message_detail(
                client, 
                message_id, 
                fetch_reply_chain=fetch_reply_chain,
                current_depth=0,
                max_depth=max_reply_depth
            )
            
            if not result:
                return f"❌ 未找到消息 (ID: {message_id})"
            
            # 处理图片注入
            if inject_images:
                injected_count = await self._inject_images_to_context(context, result)
                if injected_count > 0:
                    result["_injected_images"] = injected_count
            
            # 格式化输出
            output = self._format_output(result, inject_images)
            
            return output
            
        except ValueError as e:
            return f"❌ 消息ID格式错误：{e}"
        except Exception as e:
            logger.error(f"Error getting message detail: {e}")
            return f"❌ 获取消息详情失败：{e}"
    
    async def _get_message_detail(
        self, 
        client, 
        message_id: str, 
        fetch_reply_chain: bool = True,
        current_depth: int = 0,
        max_depth: int = 3
    ) -> Optional[Dict]:
        """获取单条消息的详细信息"""
        
        try:
            # 调用 get_msg API
            try:
                msg_id_int = int(message_id)
            except ValueError:
                # 尝试处理带下划线的 ID（如 12345_6789）
                if "_" in str(message_id):
                    msg_id_int = int(str(message_id).split("_")[0])
                else:
                    raise ValueError(f"无法解析消息ID: {message_id}")
            
            msg_data = await call_onebot(client, 'get_msg', message_id=msg_id_int)
            
            if not msg_data:
                return None
            
            # 兼容不同 OneBot 实现的响应结构
            msg_payload = msg_data.get("data", msg_data)
            
            # 提取基本信息
            result = {
                "message_id": str(msg_payload.get("message_id", message_id)),
                "time": msg_payload.get("time"),
                "message_type": msg_payload.get("message_type"),
                "sender": msg_payload.get("sender", {}),
                "group_id": msg_payload.get("group_id"),
            }
            
            # 获取原始消息段
            message_content = msg_payload.get("message", [])
            
            # 解析消息段
            parsed = self._parse_message_segments(message_content)
            result.update(parsed)
            
            # 获取回复链
            if fetch_reply_chain and current_depth < max_depth:
                reply_info = parsed.get("reply_info")
                if reply_info and reply_info.get("reply_to_msg_id"):
                    reply_msg_id = reply_info["reply_to_msg_id"]
                    try:
                        reply_detail = await self._get_message_detail(
                            client,
                            reply_msg_id,
                            fetch_reply_chain=True,
                            current_depth=current_depth + 1,
                            max_depth=max_depth
                        )
                        if reply_detail:
                            result["reply_chain"] = reply_detail
                    except Exception as e:
                        logger.debug(f"Failed to fetch reply chain for {reply_msg_id}: {e}")
                        result["reply_chain_error"] = str(e)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in _get_message_detail: {e}")
            raise
    
    def _parse_message_segments(self, message_content: Any) -> Dict:
        """解析消息段，提取各类信息"""
        
        result = {
            "segments": [],
            "summary": "",
            "files": [],
            "images": [],
            "reply_info": None,
            "forward_info": None,
            "has_image": False,
            "has_file": False,
            "has_video": False,
            "has_audio": False,
            "has_forward": False,
            "has_reply": False,
        }
        
        if not isinstance(message_content, list):
            # 可能是字符串格式的消息
            if isinstance(message_content, str):
                result["summary"] = message_content
                result["segments"] = [{"type": "text", "data": {"text": message_content}}]
            return result
        
        summary_parts = []
        
        for seg in message_content:
            if not isinstance(seg, dict):
                continue
            
            seg_type = seg.get("type", "unknown")
            seg_data = seg.get("data", {})
            
            # 保存原始 segment
            result["segments"].append(seg)
            
            # 根据类型处理
            if seg_type == "text":
                text = seg_data.get("text", "")
                summary_parts.append(text)
                
            elif seg_type == "image":
                result["has_image"] = True
                image_info = self._extract_image_info(seg_data)
                result["images"].append(image_info)
                summary_parts.append("[图片]")
                
            elif seg_type == "file":
                result["has_file"] = True
                file_info = self._extract_file_info(seg_data, "file")
                result["files"].append(file_info)
                file_name = file_info.get("name", "file")
                summary_parts.append(f"[文件:{file_name}]")
                
            elif seg_type == "video":
                result["has_video"] = True
                video_info = self._extract_file_info(seg_data, "video")
                result["files"].append(video_info)
                summary_parts.append("[视频]")
                
            elif seg_type == "record":
                result["has_audio"] = True
                audio_info = self._extract_file_info(seg_data, "audio")
                result["files"].append(audio_info)
                summary_parts.append("[语音]")
                
            elif seg_type == "reply":
                result["has_reply"] = True
                result["reply_info"] = {
                    "reply_to_msg_id": str(seg_data.get("id", "")),
                }
                summary_parts.append(f"[回复:{seg_data.get('id', '')}]")
                
            elif seg_type == "forward":
                result["has_forward"] = True
                result["forward_info"] = {
                    "forward_id": seg_data.get("id", ""),
                }
                summary_parts.append("[转发消息]")
                
            elif seg_type == "json":
                # JSON 卡片消息
                json_data = seg_data.get("data", "")
                card_info = self._parse_json_card(json_data)
                if card_info:
                    result["card_info"] = card_info
                summary_parts.append("[卡片消息]")
                
            elif seg_type == "xml":
                # XML 卡片消息
                summary_parts.append("[XML消息]")
                
            elif seg_type == "at":
                qq = seg_data.get("qq", "")
                if qq == "all":
                    summary_parts.append("@全体成员")
                else:
                    summary_parts.append(f"@{qq}")
                    
            elif seg_type == "face":
                face_id = seg_data.get("id", "")
                summary_parts.append(f"[表情:{face_id}]")
                
            elif seg_type == "mface":
                # 商城表情
                summary_parts.append("[商城表情]")
                
            elif seg_type == "poke":
                # 戳一戳
                summary_parts.append("[戳一戳]")
                
            else:
                summary_parts.append(f"[{seg_type}]")
        
        result["summary"] = "".join(summary_parts)
        
        return result
    
    def _extract_image_info(self, seg_data: Dict) -> Dict:
        """提取图片信息"""
        return {
            "type": "image",
            "file": seg_data.get("file", ""),
            "file_id": seg_data.get("file_id", ""),
            "url": seg_data.get("url", ""),
            "file_size": seg_data.get("file_size"),
            "width": seg_data.get("width"),
            "height": seg_data.get("height"),
            "file_unique": seg_data.get("file_unique", ""),
            "sub_type": seg_data.get("sub_type"),  # 0=普通图片, 1=表情包
        }
    
    def _extract_file_info(self, seg_data: Dict, file_type: str) -> Dict:
        """提取文件/视频/音频信息"""
        return {
            "type": file_type,
            "file": seg_data.get("file", ""),
            "file_id": seg_data.get("file_id", ""),
            "name": seg_data.get("name", seg_data.get("file", "")),
            "url": seg_data.get("url", ""),
            "path": seg_data.get("path", ""),
            "file_size": seg_data.get("file_size"),
            "duration": seg_data.get("duration"),  # 视频/音频时长
        }
    
    def _parse_json_card(self, json_str: str) -> Optional[Dict]:
        """尝试解析 JSON 卡片消息"""
        if not json_str:
            return None
        try:
            data = json.loads(json_str)
            # 提取一些常见字段
            return {
                "app": data.get("app", ""),
                "desc": data.get("desc", ""),
                "prompt": data.get("prompt", ""),
                "meta": data.get("meta", {}),
            }
        except (json.JSONDecodeError, TypeError):
            return None
    
    async def _download_and_convert_image(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """下载图片并根据需要转换格式
        
        Args:
            url: 图片 URL
            
        Returns:
            Tuple[base64_data_url, error_message]: 成功时返回 (data_url, None)，失败时返回 (None, error)
        """
        try:
            # 下载图片（异步）
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return None, f"下载失败: HTTP {resp.status}"
                    
                    content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
                    image_data = await resp.read()
            
            # 检测实际的图片格式
            detected_format = self._detect_image_format(image_data)
            if detected_format:
                content_type = detected_format
            
            # 检查是否需要转换
            if content_type in SUPPORTED_IMAGE_FORMATS:
                # 模型已支持的格式直接保留原始 URL，避免把图片转成巨大的 data URL 注入上下文。
                return url, None
            
            elif content_type in CONVERT_IMAGE_FORMATS or content_type.startswith('image/'):
                # 需要转换格式 - 使用线程池避免阻塞事件循环
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    _image_executor,
                    self._convert_image_sync,
                    image_data,
                    content_type
                )
                return result
            else:
                return None, f"不支持的图片格式: {content_type}"
                
        except aiohttp.ClientError as e:
            return None, f"网络错误: {e}"
        except Exception as e:
            return None, f"处理失败: {e}"
    
    def _convert_image_sync(self, image_data: bytes, content_type: str) -> Tuple[Optional[str], Optional[str]]:
        """同步图片转换（在线程池中执行）
        
        将图片转换为 PNG 格式。此方法是 CPU 密集型操作，
        应通过 run_in_executor 在线程池中调用，避免阻塞事件循环。
        
        Args:
            image_data: 原始图片二进制数据
            content_type: 原始图片的 MIME 类型
            
        Returns:
            Tuple[base64_data_url, error_message]: 成功时返回 (data_url, None)，失败时返回 (None, error)
        """
        try:
            from PIL import Image as PILImage
            
            img = PILImage.open(BytesIO(image_data))
            
            # GIF 可能有多帧，只取第一帧
            if hasattr(img, 'n_frames') and img.n_frames > 1:
                img.seek(0)
            
            # 转换为 RGB（处理 RGBA、P 等模式）
            if img.mode in ('RGBA', 'LA', 'P'):
                # 创建白色背景
                background = PILImage.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 保存为 PNG（无损）
            buffer = BytesIO()
            img.save(buffer, format='PNG', optimize=True)
            buffer.seek(0)
            
            base64_data = base64.b64encode(buffer.read()).decode('utf-8')
            logger.debug(f"Converted image from {content_type} to PNG")
            return f"data:image/png;base64,{base64_data}", None
            
        except ImportError:
            return None, "需要 PIL 库来转换图片格式"
        except Exception as e:
            return None, f"图片转换失败: {e}"
    
    def _detect_image_format(self, data: bytes) -> Optional[str]:
        """通过文件头检测图片格式"""
        if len(data) < 8:
            return None
        
        # PNG: 89 50 4E 47 0D 0A 1A 0A
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png'
        
        # JPEG: FF D8 FF
        if data[:3] == b'\xff\xd8\xff':
            return 'image/jpeg'
        
        # GIF: GIF87a or GIF89a
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return 'image/gif'
        
        # WebP: RIFF....WEBP
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'image/webp'
        
        # BMP: BM
        if data[:2] == b'BM':
            return 'image/bmp'
        
        # TIFF: II or MM
        if data[:2] in (b'II', b'MM'):
            return 'image/tiff'
        
        return None

    async def _inject_images_to_context(
        self,
        context: ContextWrapper[AstrAgentContext],
        msg_detail: Dict
    ) -> int:
        """将消息中的图片注入到 LLM 视觉上下文中
        
        会自动处理不支持的图片格式（如 GIF），将其转换为 PNG
        
        Returns:
            注入的图片数量
        """
        images = msg_detail.get("images", [])
        if not images:
            return 0
        
        injected = 0
        max_images = self.config.get("max_inject_images", 5)
        convert_unsupported = self.config.get("convert_unsupported_formats", True)
        
        try:
            messages = context.messages
            
            if not messages:
                return 0
            
            # 查找最近的 User 消息
            target_msg = None
            for msg in reversed(messages):
                if msg.role == "user":
                    target_msg = msg
                    break
            
            if not target_msg:
                return 0
            
            # 确保 content 是列表
            if isinstance(target_msg.content, str):
                from astrbot.core.agent.message import TextPart
                target_msg.content = [TextPart(text=target_msg.content)]
            
            if not isinstance(target_msg.content, list):
                return 0
            
            for img in images[:max_images]:
                url = img.get("url", "")
                if not url:
                    continue
                
                # 下载并转换图片
                if convert_unsupported:
                    data_url, error = await self._download_and_convert_image(url)
                    if error:
                        logger.warning(f"Failed to process image: {error}, url: {url[:50]}...")
                        # 如果转换失败，尝试直接使用原始 URL（可能会在 LLM 端失败）
                        data_url = url
                else:
                    data_url = url
                
                img_part = ImageURLPart(
                    image_url=ImageURLPart.ImageURL(
                        url=data_url,
                        id=f"msg_img_{img.get('file_id', '')[:8]}"
                    )
                )
                target_msg.content.append(img_part)
                injected += 1
                logger.debug(f"Injected image to context: {url[:50]}...")
            
            if injected > 0:
                logger.info(f"Injected {injected} images from message detail to LLM context")
            
        except Exception as e:
            logger.error(f"Failed to inject images: {e}")
        
        return injected
    
    def _format_output(self, result: Dict, images_injected: bool) -> str:
        """格式化输出结果"""
        
        output_parts = []
        
        # 基本信息
        output_parts.append("📨 **消息详情**\n")
        output_parts.append(f"- 消息ID: {result.get('message_id')}")
        output_parts.append(f"- 消息类型: {result.get('message_type', 'unknown')}")
        
        # 发送者信息
        sender = result.get("sender", {})
        if sender:
            sender_name = sender.get("card") or sender.get("nickname") or "Unknown"
            sender_id = sender.get("user_id", "Unknown")
            output_parts.append(f"- 发送者: {sender_name} ({sender_id})")
        
        # 时间
        if result.get("time"):
            import time
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["time"]))
            output_parts.append(f"- 时间: {time_str}")
        
        # 群号
        if result.get("group_id"):
            output_parts.append(f"- 群号: {result['group_id']}")
        
        output_parts.append("")
        
        # 内容摘要
        output_parts.append("📝 **内容摘要**")
        output_parts.append(result.get("summary", "(无内容)"))
        output_parts.append("")
        
        # 内容类型标记
        content_types = []
        if result.get("has_image"):
            content_types.append("📷 图片")
        if result.get("has_video"):
            content_types.append("🎬 视频")
        if result.get("has_audio"):
            content_types.append("🎵 语音")
        if result.get("has_file"):
            content_types.append("📎 文件")
        if result.get("has_forward"):
            content_types.append("↪️ 转发")
        if result.get("has_reply"):
            content_types.append("💬 回复")
        
        if content_types:
            output_parts.append(f"📌 **包含内容类型**: {' | '.join(content_types)}")
            output_parts.append("")
        
        # 图片详情
        images = result.get("images", [])
        if images:
            output_parts.append(f"🖼️ **图片信息** ({len(images)} 张)")
            for i, img in enumerate(images, 1):
                output_parts.append(f"  [{i}] file_id: {img.get('file_id', 'N/A')[:20]}...")
                if img.get("url"):
                    output_parts.append(f"      url: {img['url'][:60]}...")
                if img.get("file_size"):
                    size_kb = int(img["file_size"]) / 1024
                    output_parts.append(f"      size: {size_kb:.1f} KB")
                if img.get("width") and img.get("height"):
                    output_parts.append(f"      dimensions: {img['width']}x{img['height']}")
            output_parts.append("")
        
        # 文件详情
        files = result.get("files", [])
        if files:
            output_parts.append(f"📁 **文件信息** ({len(files)} 个)")
            for i, f in enumerate(files, 1):
                file_type = f.get("type", "file")
                file_name = f.get("name", "unknown")
                output_parts.append(f"  [{i}] [{file_type}] {file_name}")
                if f.get("file_id"):
                    output_parts.append(f"      file_id: {f['file_id'][:20]}...")
                if f.get("url"):
                    output_parts.append(f"      url: {f['url'][:60]}...")
                if f.get("file_size"):
                    size_mb = int(f["file_size"]) / 1024 / 1024
                    output_parts.append(f"      size: {size_mb:.2f} MB")
                if f.get("duration"):
                    output_parts.append(f"      duration: {f['duration']}s")
            output_parts.append("")
        
        # 回复信息
        reply_info = result.get("reply_info")
        if reply_info:
            output_parts.append("💬 **回复信息**")
            output_parts.append(f"  回复的消息ID: {reply_info.get('reply_to_msg_id')}")
            output_parts.append("")
        
        # 回复链
        reply_chain = result.get("reply_chain")
        if reply_chain:
            output_parts.append("🔗 **回复链**")
            chain_output = self._format_reply_chain(reply_chain, depth=1)
            output_parts.append(chain_output)
            output_parts.append("")
        
        # 卡片信息
        card_info = result.get("card_info")
        if card_info:
            output_parts.append("🃏 **卡片信息**")
            output_parts.append(f"  app: {card_info.get('app', 'N/A')}")
            output_parts.append(f"  desc: {card_info.get('desc', 'N/A')}")
            if card_info.get("prompt"):
                output_parts.append(f"  prompt: {card_info['prompt'][:100]}...")
            output_parts.append("")
        
        # 转发信息
        forward_info = result.get("forward_info")
        if forward_info:
            output_parts.append("↪️ **转发消息**")
            output_parts.append(f"  forward_id: {forward_info.get('forward_id')}")
            output_parts.append("")
        
        # 图片注入提示
        injected_count = result.get("_injected_images", 0)
        if injected_count > 0:
            output_parts.append(f"👁️ **视觉上下文**: 已将 {injected_count} 张图片注入到你的视觉上下文中，你可以直接「看到」这些图片。")
        elif images_injected and images:
            output_parts.append("ℹ️ 提示: 图片已请求注入，但可能因为 URL 无效或其他原因未能成功。")
        
        # 原始消息段（JSON 格式，用于高级用途）
        output_parts.append("")
        output_parts.append("📋 **原始消息段 (JSON)**")
        segments_json = json.dumps(result.get("segments", []), ensure_ascii=False, indent=2)
        # 限制长度
        if len(segments_json) > 1500:
            segments_json = segments_json[:1500] + "\n... (已截断)"
        output_parts.append(f"```json\n{segments_json}\n```")
        
        return "\n".join(output_parts)
    
    def _format_reply_chain(self, chain: Dict, depth: int = 1) -> str:
        """递归格式化回复链"""
        indent = "  " * depth
        parts = []
        
        sender = chain.get("sender", {})
        sender_name = sender.get("card") or sender.get("nickname") or "Unknown"
        msg_id = chain.get("message_id", "?")
        summary = chain.get("summary", "(无内容)")
        
        # 限制摘要长度
        if len(summary) > 50:
            summary = summary[:47] + "..."
        
        parts.append(f"{indent}└─ [{msg_id}] {sender_name}: {summary}")
        
        # 递归处理嵌套的回复链
        nested_chain = chain.get("reply_chain")
        if nested_chain:
            parts.append(self._format_reply_chain(nested_chain, depth + 1))
        
        return "\n".join(parts)
