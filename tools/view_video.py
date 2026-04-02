
import os
import re
import json
import time
import base64
import shutil
import aiohttp
import asyncio
import hashlib
import traceback
import urllib.parse
from functools import reduce
from typing import Optional, Dict, Tuple

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from ..utils import call_onebot, check_tool_permission, get_original_tool_name

class ViewVideoTool(FunctionTool):
    def __init__(self, plugin_instance):
        super().__init__(
            name="view_video",
            description="查看并分析视频内容。支持多种视频来源：\n"
                        "1. B站视频 - 链接/BV号\n"
                        "2. QQ视频消息 - message_id\n"
                        "3. 浏览器网页中的视频元素 - element_id\n"
                        "4. 视频直链URL - video_url\n"
                        "调用后你将获得视频内容的文本描述。",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "视频消息的ID。用于直接发送的QQ视频文件。",
                    },
                    "bilibili": {
                        "type": "string",
                        "description": "B站视频标识。支持BV号(BV1xx...)、av号(av1xx)、视频链接(bilibili.com)、短链(b23.tv)或包含这些内容的分享文本。",
                    },
                    "element_id": {
                        "type": "integer",
                        "description": "浏览器网页中视频元素的ID。需要先使用 browser_open 打开网页，然后使用截图中红色标记的元素ID。",
                    },
                    "video_url": {
                        "type": "string",
                        "description": "视频的直链URL。支持 HTTP/HTTPS 链接，如 https://example.com/video.mp4",
                    }
                },
                "required": [],
            }
        )
        self.plugin = plugin_instance
        self.config = self.plugin.config.get("gemini_video_config", {})
    
    def _format_error(self, stage: str, error: Exception, details: str = "") -> str:
        """格式化错误信息，包含阶段、错误类型、错误消息和详细信息"""
        error_type = type(error).__name__
        error_msg = str(error)
        
        result = f"❌ 视频分析失败\n"
        result += f"📍 失败阶段: {stage}\n"
        result += f"🔴 错误类型: {error_type}\n"
        result += f"💬 错误信息: {error_msg}\n"
        
        if details:
            result += f"📝 详细信息: {details}\n"
        
        # 添加常见问题提示
        if "timeout" in error_msg.lower() or isinstance(error, asyncio.TimeoutError):
            result += f"💡 提示: 请求超时，可能是视频过大或网络不稳定，请尝试增加超时时间或使用较小的视频。"
        elif "401" in error_msg or "403" in error_msg or "invalid" in error_msg.lower() and "key" in error_msg.lower():
            result += f"💡 提示: API Key 可能无效或已过期，请检查插件配置中的 Gemini API Key。"
        elif "429" in error_msg:
            result += f"💡 提示: API 请求频率过高，请稍后再试。"
        elif "500" in error_msg or "502" in error_msg or "503" in error_msg:
            result += f"💡 提示: Gemini 服务端错误，请稍后再试。"
        elif "connection" in error_msg.lower() or "network" in error_msg.lower():
            result += f"💡 提示: 网络连接错误，请检查网络状态和 API 地址配置。"
        
        return result
        
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        
        # 权限检查
        permission_config = self.plugin.config.get("tool_permission", {})
        original_name = get_original_tool_name(self.name, self.plugin.add_tool_prefix)
        
        client = getattr(event, 'bot', None)
        has_permission, reason = await check_tool_permission(
            original_name,
            event,
            permission_config,
            client
        )
        
        if not has_permission:
            return reason
        
        message_id = kwargs.get("message_id")
        bilibili_input = kwargs.get("bilibili")
        element_id = kwargs.get("element_id")
        video_url_input = kwargs.get("video_url")
        
        if not message_id and not bilibili_input and element_id is None and not video_url_input:
            return "❌ 缺少参数：请填写以下参数之一：\n" \
                   "• message_id - QQ视频消息ID\n" \
                   "• bilibili - B站视频链接/BV号\n" \
                   "• element_id - 浏览器网页中的视频元素ID\n" \
                   "• video_url - 视频直链URL"

        # 检查配置
        api_key = self.config.get("api_key")
        if not api_key:
            return "❌ 插件配置错误：未配置 Gemini API Key\n💡 提示: 请联系管理员在插件配置的 gemini_video_config 中填写 api_key。"
        
        # 获取配置项（fallback 值与 _conf_schema.json 中的 default 保持一致）
        api_url = self.config.get("api_url", "https://generativelanguage.googleapis.com").rstrip('/')
        model_id = self.config.get("model_id", "gemini-3-flash-preview")
        size_limit_mb = self.config.get("size_limit", 50)
        duration_limit = self.config.get("duration_limit", 300)
        prompt = self.config.get("prompt", "请描述视频内容")
        timeout = self.config.get("timeout", 120)
        upload_mode = self.config.get("upload_mode", "file_api")
        bilibili_quality_conf = self.config.get("bilibili_quality", "fluent")
        
        # Determine Bilibili quality
        qn = 32 if bilibili_quality_conf == "normal" else 16

        local_file_path = None
        video_url = None
        file_name = f"video_{int(time.time())}.mp4"
        bilibili_meta = {}
        download_headers = None
        
        try:
            if message_id:
                # --- QQ Video Logic ---
                event = context.context.event
                if not isinstance(event, AiocqhttpMessageEvent):
                    return "❌ 当前平台不支持此操作\n📝 原因: 仅支持 OneBot/Aiocqhttp 平台。"
                
                client = event.bot
                file_name = f"video_{message_id}.mp4"
                
                # ... (Existing QQ video logic reused) ...
                try:
                    # 尝试获取消息详情
                    try:
                        msg_data = await call_onebot(client, 'get_msg', message_id=int(message_id))
                    except ValueError as e:
                        return f"❌ 消息ID格式错误\n📝 原因: message_id 必须是有效的数字\n💬 传入值: {message_id}"
                    except Exception as e:
                        return self._format_error("获取消息详情", e, f"调用 get_msg API 失败，message_id={message_id}")
                    
                    if not msg_data:
                        return f"❌ 未找到消息\n📝 原因: 消息ID为 {message_id} 的消息不存在或已过期\n💡 提示: 请确认消息ID是否正确，历史消息可能已被清理。"
                    
                    # 兼容 NapCat 等实现，返回数据可能包裹在 data 字段中
                    msg_payload = msg_data.get("data", msg_data)
                    message_content = msg_payload.get('message', [])
                    
                    # 查找视频组件
                    video_comp = None
                    found_types = []
                    video_extensions = ('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg', '.3gp')
                    
                    if isinstance(message_content, list):
                        for comp in message_content:
                            if isinstance(comp, dict):
                                comp_type = comp.get('type', 'unknown')
                                found_types.append(comp_type)
                                if comp_type == 'video':
                                    video_comp = comp
                                    break
                                if comp_type == 'file':
                                    comp_data = comp.get('data', {})
                                    f_name = comp_data.get('file', '') or comp_data.get('name', '') or comp.get('file', '') or comp.get('name', '')
                                    if f_name and f_name.lower().endswith(video_extensions):
                                        video_comp = comp
                                        video_comp['_is_file_video'] = True
                                        logger.info(f"Detected video file sent as file type: {f_name}")
                                        break
                    
                    if not video_comp:
                        return f"❌ 该消息中未包含视频文件\n📝 消息内容类型: {found_types}"

                    # 获取视频下载链接
                    video_data_field = video_comp.get("data") or {}
                    is_file_video = video_comp.get('_is_file_video', False)
                    video_url = video_data_field.get('url') or video_comp.get('url')
                    file_id = video_data_field.get('file_id') or video_data_field.get('file') or video_comp.get('file_id') or video_comp.get('file')
                    
                    if is_file_video:
                        file_id = video_data_field.get('file_id') or video_data_field.get('file') or video_data_field.get('id') or video_comp.get('file_id') or video_comp.get('file')

                    if not video_url and file_id:
                        try:
                            file_data = await call_onebot(client, 'get_file', file_id=file_id, file=file_id)
                            file_payload = file_data.get("data", file_data)
                            video_url = file_payload.get('url') or file_payload.get('file') or file_payload.get('path')
                            if not video_url and file_payload.get('base64'):
                                video_url = f"base64://{file_payload.get('base64')}"
                        except Exception as e:
                            logger.warning(f"Failed to get_file: {e}")
                    
                    if not video_url and file_id and is_file_video:
                        try:
                            file_data = await call_onebot(client, 'download_file', url=file_id)
                            file_payload = file_data.get("data", file_data)
                            video_url = file_payload.get('file') or file_payload.get('path')
                        except Exception as e:
                            logger.debug(f"download_file API not available: {e}")

                    if not video_url:
                        return f"❌ 无法获取视频下载链接\n📝 原因: 视频文件可能已过期或 OneBot 实现不支持获取视频URL"

                except Exception as e:
                    if "❌" in str(e): return str(e)
                    logger.error(f"Error getting video info: {e}\n{traceback.format_exc()}")
                    return self._format_error("获取视频信息", e, f"message_id={message_id}")
            
            elif bilibili_input:
                # --- Bilibili Logic ---
                try:
                    # Resolve short link
                    real_input = bilibili_input
                    if "b23.tv" in bilibili_input:
                        url_match = re.search(r"(https?://b23\.tv/[^ \n]+)", bilibili_input)
                        if url_match:
                            real_input = await self._resolve_short_link(url_match.group(1))

                    bvid, aid, p = self._parse_bilibili_input(real_input)
                    if not bvid and not aid:
                        return f"❌ 无法识别的B站链接/ID\n📝 输入: {bilibili_input[:50]}...\n💡 支持: BV号、av号、视频链接、b23.tv短链"

                    # Get metadata and play url
                    bilibili_meta = await self._get_bilibili_video_data(bvid, aid, p, qn)
                    
                    # Duration Check
                    if bilibili_meta['duration'] > duration_limit:
                         return f"❌ 视频时长过长\n📝 视频时长: {bilibili_meta['duration']}秒\n📝 限制时长: {duration_limit}秒\n💡 提示: 请选择较短的视频，或让管理员调整 duration_limit。"

                    video_url = bilibili_meta['url']
                    file_name = f"video_bilibili_{bilibili_meta.get('bvid', 'unknown')}_p{p}.mp4"
                    download_headers = {
                        'Referer': 'https://www.bilibili.com/',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    
                    # Update prompt with Bilibili metadata
                    bili_info_text = f"【B站视频信息】\n标题: {bilibili_meta['title']}\nUP主: {bilibili_meta['owner']}\n简介: {bilibili_meta['desc'][:500]}\n"
                    prompt = f"{bili_info_text}\n{prompt}\n(请结合视频画面和上述B站元信息进行分析，元信息仅供参考)"

                except Exception as e:
                    logger.error(f"Error getting bilibili info: {e}\n{traceback.format_exc()}")
                    return self._format_error("获取B站信息", e)

            elif element_id is not None:
                # --- Browser Video Element Logic ---
                try:
                    from ..browser_core import browser_manager
                    
                    # 检查浏览器是否已打开
                    if not browser_manager.page:
                        return "❌ 浏览器未打开任何页面。请先使用 browser_open 打开网页，然后在截图中找到视频元素的ID。"
                    
                    # 从页面获取视频元素信息
                    video_info = await self._get_video_from_browser_element(element_id)
                    
                    if video_info.get("error"):
                        return f"❌ {video_info['error']}"
                    
                    video_url = video_info.get("src")
                    file_name = video_info.get("filename", f"video_element_{element_id}.mp4")
                    
                    if not video_url:
                        return "❌ 无法获取视频元素的播放地址。该视频可能使用了 blob URL 或需要特殊授权。"
                    
                except ImportError:
                    return "❌ 浏览器模块未启用。请确保已启用浏览器工具。"
                except Exception as e:
                    logger.error(f"Error getting video from browser element: {e}\n{traceback.format_exc()}")
                    return self._format_error("获取浏览器视频元素", e)

            elif video_url_input:
                # --- Direct Video URL Logic ---
                video_url = video_url_input.strip()
                
                # 基本 URL 验证
                if not video_url.startswith(('http://', 'https://')):
                    return "❌ 无效的视频URL。请提供以 http:// 或 https:// 开头的视频直链。"
                
                file_name = f"video_url_{int(time.time())}.mp4"

            # 2. 下载视频
            # 使用 AstrBot 数据目录下的专用临时目录，避免不同部署方式下的路径问题
            temp_dir = os.path.join(get_astrbot_data_path(), "qq_tools", "temp")
            local_file_path = os.path.join(temp_dir, file_name)
            try:
                os.makedirs(temp_dir, exist_ok=True)
            except Exception as e:
                return self._format_error("创建临时目录", e, f"路径: {temp_dir}")
            
            # ... (Download Logic) ...
            try:
                # 检查是否为 base64 格式
                if video_url.startswith("base64://"):
                    try:
                        base64_data = video_url[9:]
                        video_bytes = base64.b64decode(base64_data)
                        with open(local_file_path, 'wb') as f:
                            f.write(video_bytes)
                        logger.info(f"Video saved from base64: {local_file_path}")
                    except Exception as e:
                        return self._format_error("解码 Base64 视频", e)
                elif self._is_local_path(video_url):
                    # 本地文件路径
                    try:
                        decoded_path = urllib.parse.unquote(video_url)
                        logger.info(f"Detected local file path: {decoded_path}")
                        
                        if not os.path.exists(decoded_path):
                            return f"❌ 本地视频文件不存在\n📝 路径: {decoded_path}"
                        
                        source_size = os.path.getsize(decoded_path)
                        source_size_mb = source_size / 1024 / 1024
                        if source_size_mb > size_limit_mb:
                            return f"❌ 视频文件过大\n📝 文件大小: {source_size_mb:.2f}MB\n📝 限制大小: {size_limit_mb}MB"
                        
                        shutil.copy2(decoded_path, local_file_path)
                        logger.info(f"Video copied from local path: {decoded_path} -> {local_file_path}")
                    except Exception as e:
                        return self._format_error("复制本地视频文件", e, f"源路径: {video_url}")
                else:
                    # 正常 HTTP 下载 (Added headers support for Bilibili)
                    async with aiohttp.ClientSession() as session:
                        try:
                            req_headers = download_headers or {}
                            async with session.get(video_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                                if resp.status != 200:
                                    return f"❌ 下载视频失败\n📝 HTTP状态码: {resp.status}\n💬 URL: {video_url[:100]}..."
                                
                                content_length = resp.headers.get('Content-Length')
                                if content_length:
                                    size_mb = int(content_length) / 1024 / 1024
                                    if size_mb > size_limit_mb:
                                        return f"❌ 视频文件过大\n📝 文件大小: {size_mb:.2f}MB\n📝 限制大小: {size_limit_mb}MB"
                                
                                with open(local_file_path, 'wb') as f:
                                    downloaded = 0
                                    while True:
                                        chunk = await resp.content.read(8192)
                                        if not chunk:
                                            break
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        if downloaded > size_limit_mb * 1024 * 1024:
                                            raise Exception(f"下载过程中超出大小限制 ({size_limit_mb}MB)")
                        except asyncio.TimeoutError:
                            return f"❌ 下载视频超时\n📝 超时时间: 120秒"

                actual_size = os.path.getsize(local_file_path)
                actual_size_mb = actual_size / 1024 / 1024
                if actual_size_mb > size_limit_mb:
                    os.remove(local_file_path)
                    return f"❌ 视频文件过大\n📝 实际大小: {actual_size_mb:.2f}MB\n📝 限制大小: {size_limit_mb}MB"
                
                logger.info(f"Video downloaded successfully: {local_file_path} ({actual_size_mb:.2f}MB)")
                
            except Exception as e:
                if os.path.exists(local_file_path):
                    try: os.remove(local_file_path)
                    except: pass
                if "❌" in str(e): return str(e)
                logger.error(f"Error downloading video: {e}\n{traceback.format_exc()}")
                return self._format_error("下载视频", e, f"URL: {video_url[:100]}...")

            # 3. 根据配置选择上传方式
            try:
                if upload_mode == "file_api":
                    result_text, error_info = await self._process_with_file_api(
                        api_url, api_key, model_id, local_file_path, prompt, timeout
                    )
                else:
                    result_text, error_info = await self._process_with_inline_base64(
                        api_url, api_key, model_id, local_file_path, prompt, timeout
                    )
                
                if error_info:
                    return error_info
                
                final_result = ""
                if bilibili_meta:
                    final_result += f"【B站信息】\n📺 标题: {bilibili_meta['title']}\n👤 UP主: {bilibili_meta['owner']}\n⏱️ 时长: {bilibili_meta['duration']}秒\n📝 简介: {bilibili_meta['desc'][:200]}...\n\n"
                
                final_result += f"✅ Gemini 分析结果：\n{result_text}"
                return final_result
                
            except Exception as e:
                logger.error(f"Error processing with Gemini: {e}\n{traceback.format_exc()}")
                return self._format_error("Gemini API 调用", e, f"模型: {model_id}, API地址: {api_url}, 上传方式: {upload_mode}")
        finally:
            # 清理临时文件
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.remove(local_file_path)
                    logger.debug(f"Cleaned up temp file: {local_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temp file: {e}")

    def _is_local_path(self, path: str) -> bool:
        """检测给定的字符串是否为本地文件路径而非 URL"""
        if not path:
            return False
        
        # URL 编码的路径需要先解码
        decoded_path = urllib.parse.unquote(path)
        
        # 检查是否为 Windows 绝对路径 (如 C:\..., D:\..., c%3A\...)
        if len(decoded_path) >= 2:
            # Windows 路径: C:\ 或 C:/
            if decoded_path[1] == ':' and (len(decoded_path) == 2 or decoded_path[2] in ('\\', '/')):
                return True
        
        # 检查是否为 Unix 绝对路径 (如 /home/...)
        if decoded_path.startswith('/') and not decoded_path.startswith('//'):
            # 排除网络路径 //server/share
            return True
        
        # 检查是否包含典型的 URL scheme
        if path.startswith(('http://', 'https://', 'ftp://', 'file://')):
            return False
        
        # 检查是否看起来像本地路径（包含反斜杠或路径分隔符但不是 URL）
        if ('\\' in decoded_path or '%5C' in path.upper() or '%5c' in path.lower()):
            return True
        
        return False

    def _get_mime_type(self, file_path: str) -> str:
        """根据文件扩展名获取 MIME 类型"""
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            '.mp4': 'video/mp4',
            '.avi': 'video/x-msvideo',
            '.mkv': 'video/x-matroska',
            '.mov': 'video/quicktime',
            '.wmv': 'video/x-ms-wmv',
            '.flv': 'video/x-flv',
            '.webm': 'video/webm',
            '.m4v': 'video/x-m4v',
            '.mpeg': 'video/mpeg',
            '.mpg': 'video/mpeg',
            '.3gp': 'video/3gpp',
        }
        return mime_map.get(ext, 'video/mp4')

    def _parse_generate_response(self, resp_text: str, model_id: str) -> Tuple[Optional[str], Optional[str]]:
        """解析 Gemini generateContent 响应
        
        Returns:
            tuple: (result_text, error_info) - 成功时 error_info 为 None，失败时 result_text 为 None
        """
        try:
            result = json.loads(resp_text)
        except json.JSONDecodeError as e:
            return None, f"❌ 解析 Gemini 响应失败\n🔴 错误类型: JSON解析错误\n💬 错误信息: {e}\n📝 响应内容: {resp_text[:200]}..."
        
        try:
            # 检查是否有安全过滤
            if "promptFeedback" in result:
                block_reason = result["promptFeedback"].get("blockReason")
                if block_reason:
                    safety_ratings = result["promptFeedback"].get("safetyRatings", [])
                    ratings_str = ", ".join([f"{r.get('category', 'unknown')}: {r.get('probability', 'unknown')}" for r in safety_ratings])
                    return None, f"❌ 内容被 Gemini 安全过滤器拦截\n📝 拦截原因: {block_reason}\n📝 安全评级: {ratings_str}\n💡 提示: 视频内容可能包含敏感信息，请尝试其他视频。"
            
            # 检查是否有候选结果
            if "candidates" not in result or len(result["candidates"]) == 0:
                return None, f"❌ Gemini 未返回任何结果\n📝 响应内容: {json.dumps(result, ensure_ascii=False)[:300]}...\n💡 提示: 可能是视频无法被模型处理。"
            
            candidate = result["candidates"][0]
            
            # 检查候选结果的完成原因
            finish_reason = candidate.get("finishReason", "")
            if finish_reason == "SAFETY":
                safety_ratings = candidate.get("safetyRatings", [])
                ratings_str = ", ".join([f"{r.get('category', 'unknown')}: {r.get('probability', 'unknown')}" for r in safety_ratings])
                return None, f"❌ 生成内容被安全过滤器拦截\n📝 完成原因: {finish_reason}\n📝 安全评级: {ratings_str}\n💡 提示: 生成的内容可能包含敏感信息。"
            elif finish_reason == "RECITATION":
                return None, f"❌ 生成内容因版权问题被拦截\n📝 完成原因: {finish_reason}\n💡 提示: 视频可能包含受版权保护的内容。"
            elif finish_reason not in ["STOP", "MAX_TOKENS", ""]:
                return None, f"❌ 生成异常终止\n📝 完成原因: {finish_reason}\n💡 提示: 请尝试其他视频或调整提示词。"
            
            # 提取文本内容
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            
            if not parts:
                return None, f"❌ Gemini 返回空内容\n📝 候选结果: {json.dumps(candidate, ensure_ascii=False)[:300]}...\n💡 提示: 模型可能无法处理此视频。"
            
            text_content = parts[0].get("text", "")
            if not text_content:
                return None, f"❌ Gemini 返回空文本\n📝 响应部分: {json.dumps(parts, ensure_ascii=False)[:300]}...\n💡 提示: 模型可能无法描述此视频内容。"
            
            return text_content, None
            
        except (KeyError, IndexError) as e:
            return None, f"❌ 解析 Gemini 结果失败\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n📝 响应内容: {json.dumps(result, ensure_ascii=False)[:300]}..."

    async def _process_with_file_api(self, api_base: str, api_key: str, model_id: str,
                                     file_path: str, prompt: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
        """使用 Gemini File API 上传视频并生成内容（Resumable Upload 协议）
        
        File API 流程（两步上传协议）:
        1. 发起上传请求，获取 upload_url
        2. 向 upload_url 上传实际文件数据
        3. 等待文件处理完成
        4. 调用生成接口
        5. 删除上传的文件（可选）
        
        Returns:
            tuple: (result_text, error_info) - 成功时 error_info 为 None，失败时 result_text 为 None
        """
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / 1024 / 1024
        mime_type = self._get_mime_type(file_path)
        display_name = os.path.basename(file_path)
        
        logger.info(f"Using File API (Resumable Upload) to upload: {file_path} ({file_size_mb:.2f}MB, {mime_type})")
        
        uploaded_file_uri = None
        uploaded_file_name = None
        
        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: 发起上传请求，获取 upload_url
                init_url = f"{api_base}/upload/v1beta/files?key={api_key}"
                
                init_headers = {
                    'X-Goog-Upload-Protocol': 'resumable',
                    'X-Goog-Upload-Command': 'start',
                    'X-Goog-Upload-Header-Content-Length': str(file_size),
                    'X-Goog-Upload-Header-Content-Type': mime_type,
                    'Content-Type': 'application/json',
                }
                
                init_body = json.dumps({
                    'file': {
                        'display_name': display_name
                    }
                })
                
                logger.info(f"Step 1: Initiating resumable upload...")
                
                try:
                    async with session.post(
                        init_url,
                        headers=init_headers,
                        data=init_body,
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status != 200:
                            resp_text = await resp.text()
                            error_msg = f"❌ 初始化上传失败\n📝 HTTP状态码: {resp.status}\n💬 响应: {resp_text[:500]}\n"
                            if resp.status == 400:
                                error_msg += "💡 提示: 请求格式错误，可能是视频格式不支持。"
                            elif resp.status == 401:
                                error_msg += "💡 提示: API Key 无效，请检查配置。"
                            return None, error_msg
                        
                        # 从响应 header 获取 upload_url
                        upload_url = resp.headers.get('X-Goog-Upload-URL') or resp.headers.get('x-goog-upload-url')
                        
                        if not upload_url:
                            resp_text = await resp.text()
                            return None, f"❌ 未获取到上传URL\n📝 响应头: {dict(resp.headers)}\n📝 响应体: {resp_text[:300]}..."
                        
                        logger.info(f"Got upload URL: {upload_url[:100]}...")
                        
                except asyncio.TimeoutError:
                    return None, f"❌ 初始化上传超时\n📝 超时时间: 60秒\n💡 提示: 请检查网络连接。"
                
                # Step 2: 向 upload_url 上传实际文件数据
                logger.info(f"Step 2: Uploading file data ({file_size_mb:.2f}MB)...")
                
                # 读取文件内容
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                
                upload_headers = {
                    'Content-Length': str(file_size),
                    'X-Goog-Upload-Offset': '0',
                    'X-Goog-Upload-Command': 'upload, finalize',
                }
                
                try:
                    async with session.post(
                        upload_url,
                        headers=upload_headers,
                        data=file_data,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        resp_text = await resp.text()
                        
                        if resp.status != 200:
                            error_msg = f"❌ 文件上传失败\n📝 HTTP状态码: {resp.status}\n💬 响应: {resp_text[:500]}\n"
                            if resp.status == 400:
                                error_msg += "💡 提示: 请求格式错误，可能是视频格式不支持。"
                            elif resp.status == 413:
                                error_msg += "💡 提示: 文件过大，请尝试使用较小的视频。"
                            elif resp.status == 401:
                                error_msg += "💡 提示: API Key 无效，请检查配置。"
                            return None, error_msg
                        
                        try:
                            upload_result = json.loads(resp_text)
                            uploaded_file_name = upload_result.get("file", {}).get("name", "")
                            uploaded_file_uri = upload_result.get("file", {}).get("uri", "")
                            file_state = upload_result.get("file", {}).get("state", "")
                            
                            logger.info(f"File uploaded: name={uploaded_file_name}, uri={uploaded_file_uri}, state={file_state}")
                            
                        except json.JSONDecodeError:
                            return None, f"❌ 解析上传响应失败\n📝 响应内容: {resp_text[:300]}..."
                            
                except asyncio.TimeoutError:
                    return None, f"❌ 文件上传超时\n📝 超时时间: {timeout}秒\n📝 文件大小: {file_size_mb:.2f}MB\n💡 提示: 请尝试增加超时时间或使用较小的视频。"
                
                # Step 2: 等待文件处理完成
                if uploaded_file_name:
                    max_wait_time = timeout
                    wait_interval = 5
                    total_waited = 0
                    
                    while total_waited < max_wait_time:
                        check_url = f"{api_base}/v1beta/{uploaded_file_name}?key={api_key}"
                        
                        try:
                            async with session.get(check_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                if resp.status == 200:
                                    status_result = await resp.json()
                                    file_state = status_result.get("state", "")
                                    
                                    logger.info(f"File state: {file_state} (waited {total_waited}s)")
                                    
                                    if file_state == "ACTIVE":
                                        break
                                    elif file_state == "FAILED":
                                        error_detail = status_result.get("error", {})
                                        return None, f"❌ 文件处理失败\n📝 状态: {file_state}\n💬 错误: {error_detail}\n💡 提示: 视频格式可能不支持，请尝试 MP4 格式。"
                                    
                        except Exception as e:
                            logger.warning(f"Error checking file status: {e}")
                        
                        await asyncio.sleep(wait_interval)
                        total_waited += wait_interval
                    
                    if total_waited >= max_wait_time:
                        return None, f"❌ 等待文件处理超时\n📝 已等待: {total_waited}秒\n💡 提示: 视频可能过大，处理时间过长。请尝试较短的视频或增加超时时间。"
                
                # Step 3: 调用生成接口
                generate_url = f"{api_base}/v1beta/models/{model_id}:generateContent?key={api_key}"
                
                payload = {
                    "contents": [{
                        "parts": [
                            {"text": prompt},
                            {
                                "file_data": {
                                    "mime_type": mime_type,
                                    "file_uri": uploaded_file_uri
                                }
                            }
                        ]
                    }]
                }
                
                logger.info(f"Calling generateContent with file_uri: {uploaded_file_uri}")
                
                try:
                    async with session.post(
                        generate_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        resp_text = await resp.text()
                        
                        if resp.status != 200:
                            try:
                                error_json = json.loads(resp_text)
                                error_message = error_json.get("error", {}).get("message", resp_text)
                                error_code = error_json.get("error", {}).get("code", resp.status)
                            except:
                                error_message = resp_text[:500]
                                error_code = resp.status
                            
                            return None, f"❌ Gemini API 请求失败\n📝 HTTP状态码: {resp.status}\n📝 错误代码: {error_code}\n💬 错误信息: {error_message}"
                        
                        # 解析响应
                        return self._parse_generate_response(resp_text, model_id)
                        
                except asyncio.TimeoutError:
                    return None, f"❌ 生成请求超时\n📝 超时时间: {timeout}秒\n💡 提示: 视频可能过大，处理时间过长。"
                    
        except aiohttp.ClientError as e:
            return None, f"❌ 网络请求错误\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n💡 提示: 请检查网络连接和 API 地址配置。"
        except Exception as e:
            return None, f"❌ File API 调用异常\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n📝 API地址: {api_base}\n📝 模型: {model_id}"
        finally:
            # 尝试删除上传的文件（可选，失败不影响结果）
            if uploaded_file_name:
                try:
                    delete_url = f"{api_base}/v1beta/{uploaded_file_name}?key={api_key}"
                    async with aiohttp.ClientSession() as session:
                        async with session.delete(delete_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                logger.info(f"Deleted uploaded file: {uploaded_file_name}")
                            else:
                                logger.debug(f"Failed to delete uploaded file: {resp.status}")
                except Exception as e:
                    logger.debug(f"Error deleting uploaded file: {e}")

    async def _process_with_inline_base64(self, api_base: str, api_key: str, model_id: str,
                                          file_path: str, prompt: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
        """读取文件并转换为 Base64，使用 inlineData 上传到 Gemini 并生成内容
        
        Returns:
            tuple: (result_text, error_info) - 成功时 error_info 为 None，失败时 result_text 为 None
        """
        
        # 1. Read and Encode File
        try:
            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / 1024 / 1024
            logger.info(f"Encoding video file: {file_path} ({file_size_mb:.2f}MB)")
            
            with open(file_path, "rb") as video_file:
                video_data = video_file.read()
                base64_data = base64.b64encode(video_data).decode("utf-8")
                
            base64_size_mb = len(base64_data) / 1024 / 1024
            logger.info(f"Base64 encoded size: {base64_size_mb:.2f}MB")
            
        except MemoryError as e:
            error_msg = f"❌ 内存不足\n📝 原因: 视频文件过大，无法加载到内存进行 Base64 编码\n📝 文件大小: {file_size_mb:.2f}MB\n💡 提示: 请使用较小的视频文件（建议小于 50MB）。"
            return None, error_msg
        except Exception as e:
            error_msg = f"❌ 读取视频文件失败\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n📝 文件路径: {file_path}"
            return None, error_msg

        # 2. Generate Content with inlineData
        generate_url = f"{api_base}/v1beta/models/{model_id}:generateContent?key={api_key}"
        mime_type = self._get_mime_type(file_path)
        
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64_data
                        }
                    }
                ]
            }]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                logger.info(f"Sending request to Gemini API: {model_id}, timeout={timeout}s")
                
                try:
                    async with session.post(
                        generate_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        resp_text = await resp.text()
                        
                        if resp.status != 200:
                            # 解析错误响应
                            try:
                                error_json = json.loads(resp_text)
                                error_message = error_json.get("error", {}).get("message", resp_text)
                                error_code = error_json.get("error", {}).get("code", resp.status)
                            except:
                                error_message = resp_text[:500]
                                error_code = resp.status
                            
                            error_msg = f"❌ Gemini API 请求失败\n📝 HTTP状态码: {resp.status}\n📝 错误代码: {error_code}\n💬 错误信息: {error_message}\n"
                            
                            # 添加针对性提示
                            if resp.status == 400:
                                error_msg += "💡 提示: 请求格式错误，可能是视频格式不支持或文件损坏。"
                            elif resp.status == 401:
                                error_msg += "💡 提示: API Key 无效，请检查配置。"
                            elif resp.status == 403:
                                error_msg += "💡 提示: API Key 没有访问权限，或该地区不支持此服务。"
                            elif resp.status == 404:
                                error_msg += f"💡 提示: 模型 {model_id} 不存在，请检查 model_id 配置。"
                            elif resp.status == 429:
                                error_msg += "💡 提示: API 配额已用尽或请求过于频繁，请稍后再试。"
                            elif resp.status >= 500:
                                error_msg += "💡 提示: Gemini 服务端错误，请稍后再试。"
                            
                            return None, error_msg
                        
                        # 解析响应
                        return self._parse_generate_response(resp_text, model_id)
                            
                except asyncio.TimeoutError:
                    error_msg = f"❌ Gemini API 请求超时\n📝 超时时间: {timeout}秒\n💡 提示: 视频可能过大，处理时间过长。可尝试：\n  1. 使用较小的视频\n  2. 增加 timeout 配置值\n  3. 检查网络连接"
                    return None, error_msg
                    
        except aiohttp.ClientError as e:
            error_msg = f"❌ 网络请求错误\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n💡 提示: 请检查网络连接和 API 地址配置。"
            return None, error_msg
        except Exception as e:
            error_msg = f"❌ Gemini API 调用异常\n🔴 错误类型: {type(e).__name__}\n💬 错误信息: {e}\n📝 API地址: {api_base}\n📝 模型: {model_id}"
            return None, error_msg

    # --- Browser Video Element Helper Methods ---

    async def _get_video_from_browser_element(self, element_id: int) -> Dict:
        """从浏览器页面获取视频元素信息
        
        Args:
            element_id: 元素 ID (data-ai-id)
            
        Returns:
            dict: {'src': video_url, 'filename': name, 'poster': poster_url} 或 {'error': message}
        """
        from ..browser_core import browser_manager
        
        if not browser_manager.page:
            return {"error": "浏览器未初始化"}
        
        try:
            target_frame = None
            
            # 遍历所有 Frames 查找元素
            for frame in browser_manager.page.frames:
                try:
                    if frame.is_detached():
                        continue
                    element = await frame.query_selector(f'[data-ai-id="{element_id}"]')
                    if element:
                        target_frame = frame
                        break
                except Exception:
                    continue
            
            if not target_frame:
                return {"error": f"未找到 ID 为 {element_id} 的元素。"}
            
            # 获取视频元素信息
            result = await target_frame.evaluate(f"""
                () => {{
                    const el = document.querySelector('[data-ai-id="{element_id}"]');
                    if (!el) return {{ error: '未找到元素' }};
                    
                    const tagName = el.tagName.toLowerCase();
                    
                    // 检查是否为 video 标签
                    if (tagName === 'video') {{
                        // 优先获取 currentSrc，其次 src
                        let src = el.currentSrc || el.src || el.getAttribute('src');
                        
                        // 如果是 blob URL，尝试从 source 标签获取
                        if (!src || src.startsWith('blob:')) {{
                            const sourceEl = el.querySelector('source');
                            if (sourceEl) {{
                                src = sourceEl.src || sourceEl.getAttribute('src');
                            }}
                        }}
                        
                        // 尝试从 data 属性获取
                        if (!src || src.startsWith('blob:')) {{
                            src = el.getAttribute('data-src') ||
                                  el.getAttribute('data-video-src') ||
                                  el.getAttribute('data-url');
                        }}
                        
                        return {{
                            src: src || null,
                            poster: el.poster || null,
                            filename: src ? src.split('/').pop().split('?')[0] : null,
                            duration: el.duration || null,
                            tagName: tagName,
                            isBlob: src ? src.startsWith('blob:') : false
                        }};
                    }}
                    
                    // 检查是否为 iframe 包含视频播放器
                    if (tagName === 'iframe') {{
                        return {{
                            error: '该元素是 iframe 嵌入的视频播放器。请尝试获取 iframe 内的视频元素，或直接使用视频源地址。',
                            tagName: tagName
                        }};
                    }}
                    
                    // 检查子元素中是否有 video
                    const childVideo = el.querySelector('video');
                    if (childVideo) {{
                        let src = childVideo.currentSrc || childVideo.src;
                        if (!src || src.startsWith('blob:')) {{
                            const sourceEl = childVideo.querySelector('source');
                            if (sourceEl) {{
                                src = sourceEl.src;
                            }}
                        }}
                        return {{
                            src: src || null,
                            poster: childVideo.poster || null,
                            filename: src ? src.split('/').pop().split('?')[0] : null,
                            tagName: 'video',
                            isBlob: src ? src.startsWith('blob:') : false
                        }};
                    }}
                    
                    return {{
                        error: `该元素不是视频元素 (实际类型: ${{tagName}})。请选择页面上的 video 元素。`,
                        tagName: tagName
                    }};
                }}
            """)
            
            # 处理 blob URL
            if result.get("isBlob"):
                return {"error": "该视频使用了 blob URL，无法直接下载。请尝试使用其他方式获取视频，或查找视频的真实源地址。"}
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to get video from element {element_id}: {e}")
            return {"error": f"获取视频信息失败: {e}"}

    # --- Bilibili Helper Methods ---

    async def _resolve_short_link(self, url: str) -> str:
        """Resolve short link (like b23.tv) to get real URL"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True, timeout=10) as resp:
                    return str(resp.url)
        except:
            return url

    def _parse_bilibili_input(self, text: str) -> Tuple[Optional[str], Optional[str], int]:
        """Parse input text to get bvid or aid, and p"""
        p = 1
        
        # Extract P from text if exists ?p=2 or &p=2
        p_match = re.search(r"[?&]p=(\d+)", text)
        if p_match:
            try:
                p = int(p_match.group(1))
            except:
                pass

        # Try extract BV
        bv_match = re.search(r"(BV[0-9A-Za-z]{10})", text, re.I)
        if bv_match:
            return bv_match.group(1), None, p
        
        # Try extract av
        av_match = re.search(r"(?:av)(\d+)", text, re.I)
        if av_match:
            return None, av_match.group(1), p
            
        # Try extract URL
        url_match = re.search(r"(https?://(?:www\.|m\.|)bilibili\.com/[^ \n]+|https?://b23\.tv/[^ \n]+)", text)
        if url_match:
            url = url_match.group(1)
            return "URL:" + url, None, p
            
        return None, None, p

    def _get_mixin_key(self, orig: str) -> str:
        """WBI signature mixin key generation"""
        mixin_key_enc_tab = [
            46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
            33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
            61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
            36, 20, 34, 44, 52
        ]
        return reduce(lambda s, i: s + orig[i], mixin_key_enc_tab, "")[:32]

    def _enc_wbi(self, params: Dict, img_key: str, sub_key: str) -> Dict:
        """Sign params with WBI"""
        mixin_key = self._get_mixin_key(img_key + sub_key)
        curr_time = round(time.time())
        params['wts'] = curr_time
        params = dict(sorted(params.items()))
        # Filter invalid chars
        params = {
            k: ''.join(filter(lambda c: c not in "!'()*", str(v)))
            for k, v in params.items()
        }
        query = urllib.parse.urlencode(params)
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
        params['w_rid'] = w_rid
        return params

    async def _get_wbi_keys(self, session: aiohttp.ClientSession) -> Tuple[str, str]:
        """Get WBI keys from nav endpoint"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://www.bilibili.com/'
        }
        async with session.get("https://api.bilibili.com/x/web-interface/nav", headers=headers) as resp:
            data = await resp.json()
            wbi_img = data['data']['wbi_img']
            img_url = wbi_img['img_url']
            sub_url = wbi_img['sub_url']
            img_key = img_url.rsplit('/', 1)[1].split('.')[0]
            sub_key = sub_url.rsplit('/', 1)[1].split('.')[0]
            return img_key, sub_key

    async def _get_bilibili_video_data(self, bvid: str, aid: str, p: int, qn: int) -> Dict:
        """Get Bilibili video metadata and download URL"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://www.bilibili.com/'
        }
        
        async with aiohttp.ClientSession() as session:
            # 1. View API
            params = {}
            if bvid: params['bvid'] = bvid
            if aid: params['aid'] = aid
            
            async with session.get("https://api.bilibili.com/x/web-interface/view", params=params, headers=headers) as resp:
                view_data = await resp.json()
                if view_data['code'] != 0:
                    raise Exception(f"B站API错误: {view_data['message']}")
                
                data = view_data['data']
                title = data['title']
                desc = data['desc']
                duration = data['duration'] # seconds
                pages = data.get('pages', [])
                owner_name = data.get('owner', {}).get('name', 'Unknown')
                
                # Check P
                if p > len(pages):
                    raise Exception(f"P号超出范围 (最大P{len(pages)})")
                
                # Find cid
                current_page = next((x for x in pages if x['page'] == p), pages[0])
                cid = current_page['cid']
                part_name = current_page['part']
                
                # If duration in part is available, use it (sometimes main duration is total?)
                if 'duration' in current_page:
                    duration = current_page['duration']

            # 2. Get WBI Keys
            try:
                img_key, sub_key = await self._get_wbi_keys(session)
            except Exception as e:
                logger.warning(f"Failed to get WBI keys: {e}, will try unsigned playurl")
                img_key, sub_key = None, None

            # 3. PlayURL
            play_params = {
                'bvid': bvid or "",
                'cid': cid,
                'qn': qn,
                'fnval': 1, # mp4
                'fnver': 0,
                'fourk': 1
            }
            if aid: play_params['avid'] = aid
            
            signed_params = play_params
            if img_key and sub_key:
                signed_params = self._enc_wbi(play_params, img_key, sub_key)
            
            play_url = "https://api.bilibili.com/x/player/wbi/playurl"
            
            async with session.get(play_url, params=signed_params, headers=headers) as resp:
                play_data = await resp.json()
                if play_data['code'] != 0:
                    raise Exception(f"获取播放地址失败: {play_data['message']}")
                
                durl = play_data['data']['durl']
                if not durl:
                    raise Exception("未找到MP4播放地址")
                
                video_url = durl[0]['url']
                
                return {
                    'url': video_url,
                    'title': title,
                    'desc': desc,
                    'owner': owner_name,
                    'duration': duration,
                    'part': part_name,
                    'bvid': bvid,
                    'p': p,
                    'cid': cid
                }
