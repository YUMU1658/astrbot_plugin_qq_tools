import re
import json
import time
import asyncio
import os
import uuid
from collections import deque
from typing import Dict, Optional, List, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api import message_components as Comp
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
# FunctionTool 使用官方 API 导入
from astrbot.api import FunctionTool

from .utils import parse_at_content, parse_leaked_tool_call, call_onebot
from .tools.get_user_info import GetUserInfoTool
from .tools.get_recent_messages import GetRecentMessagesTool
from .tools.reply_message import ReplyMessageTool
from .tools.delete_message import DeleteMessageTool
from .tools.refresh_messages import RefreshMessagesTool
from .tools.stop_conversation import StopConversationTool
from .tools.poke import PokeTool
from .tools.change_group_card import ChangeGroupCardTool
from .tools.ban_user import BanUserTool
from .tools.group_ban import GroupBanTool
from .tools.group_mute_all import GroupMuteAllTool
from .tools.kick_user import KickUserTool
from .tools.get_group_member_list import GetGroupMemberListTool
from .tools.send_group_notice import SendGroupNoticeTool
from .tools.view_avatar import ViewAvatarTool
from .tools.set_essence_message import SetEssenceMessageTool
from .tools.set_special_title import SetSpecialTitleTool
from .tools.view_video import ViewVideoTool
from .tools.repeat_message import RepeatMessageTool
from .tools.get_message_detail import GetMessageDetailTool
from .tools.wake_schedule import WakeScheduleTool
from .tools.wake_manage import WakeManageTool
from .wake_scheduler import WakeScheduler, WakeTask
from .tools.browser import (
    BrowserOpenTool,
    BrowserClickTool,
    BrowserClickCoordinateTool,
    BrowserClickInElementTool,  # 新增：元素内相对位置点击
    BrowserInputTool,
    BrowserScrollTool,
    BrowserGetLinkTool,
    BrowserViewImageTool,
    BrowserScreenshotTool,
    BrowserCloseTool,
    BrowserWaitTool,
    BrowserSendImageTool,
    BrowserCropTool,  # 新增：裁剪放大区域
)

class QQToolsPlugin(Star):
    def __init__(self, context: Context, config: Dict):
        super().__init__(context)
        self.config = config
        self.tool_config = self.config.get("tools", {})
        self.general_config = self.config.get("general", {})
        self.compatibility_config = self.config.get("compatibility", {})
        
        # 工具名称前缀配置
        self.add_tool_prefix = self.compatibility_config.get("add_tool_prefix", False)
        self.tool_prefix = "qts_" if self.add_tool_prefix else ""
        
        # delay_append_msg_id 配置将在 _on_message_internal 中处理
        # 不再尝试修改 handler priority（这种方式不稳定且容易找错 handler）
        
        self.cache_size = self.general_config.get("cache_size", 50)
        
        # 消息缓存: {session_id: deque([message_info])}
        # session_id 通常是 group_id 或 user_id
        # 使用普通 dict 而非 defaultdict，以便更好地控制和清理
        self.message_cache: Dict[str, deque] = {}
        
        # 缓存最后活跃时间: {session_id: timestamp}
        # 用于定期清理不活跃的会话缓存，防止内存泄漏
        self.cache_last_active: Dict[str, float] = {}
        
        # 缓存清理配置
        self.cache_inactive_timeout = self.general_config.get("cache_inactive_timeout", 3600)  # 默认 1 小时
        self.cache_cleanup_interval = self.general_config.get("cache_cleanup_interval", 300)  # 默认 5 分钟
        
        # Poke notice 缓存：存储最近的 poke notice 事件，用于 PokeTool 获取戳一戳文案
        # 使用全局缓存而非 session 级别，因为 poke notice 的 session_id 可能与触发工具的 session_id 不同
        self.poke_notice_cache: deque = deque(maxlen=20)  # 只保留最近 20 条
        
        logger.info(f"QQToolsPlugin loaded. Cache size: {self.cache_size}, inactive timeout: {self.cache_inactive_timeout}s.")

        # 注册 FunctionTool
        self._manage_tool("user_info", GetUserInfoTool())
        self._manage_tool("search", GetRecentMessagesTool(self))
        self._manage_tool("reply", ReplyMessageTool(self))
        self._manage_tool("delete", DeleteMessageTool(self))
        self._manage_tool("refresh", RefreshMessagesTool(self))
        self._manage_tool("stop", StopConversationTool())
        self._manage_tool("poke", PokeTool(self))
        self._manage_tool("change_card", ChangeGroupCardTool(self))
        self._manage_tool("ban", BanUserTool(self), default=False)
        self._manage_tool("group_ban", GroupBanTool(self), default=False)
        self._manage_tool("group_mute_all", GroupMuteAllTool(self), default=False)
        self._manage_tool("kick_user", KickUserTool(self), default=False)
        self._manage_tool("get_member_list", GetGroupMemberListTool())
        self._manage_tool("send_notice", SendGroupNoticeTool(self), default=False)
        self._manage_tool("view_avatar", ViewAvatarTool(self))
        self._manage_tool("set_essence", SetEssenceMessageTool(self))
        self._manage_tool("set_title", SetSpecialTitleTool(self))
        self._manage_tool("view_video", ViewVideoTool(self), default=False)
        self._manage_tool("repeat", RepeatMessageTool())
        self._manage_tool("message_detail", GetMessageDetailTool(self))

        # 浏览器工具
        self._manage_browser_tools()

        # 唤醒调度器
        self.wake_scheduler: Optional[WakeScheduler] = None
        self._manage_wake_tools()

        self.check_ban_task = asyncio.create_task(self.check_ban_expiration())
        self.cache_cleanup_task = asyncio.create_task(self._cleanup_inactive_caches_loop())

    def _manage_tool(self, key: str, tool_instance: FunctionTool, default: bool = True):
        # 获取原始工具名称
        original_name = tool_instance.name
        
        # 计算当前名称和相反前缀的名称（用于清理残余）
        if self.add_tool_prefix:
            current_name = f"{self.tool_prefix}{original_name}"
            legacy_name = original_name  # 无前缀版本是残余
        else:
            current_name = original_name
            legacy_name = f"qts_{original_name}"  # 带前缀版本是残余
        
        # 修改工具实例的名称为当前配置
        tool_instance.name = current_name
        
        if self.tool_config.get(key, default):
            # 注册当前版本的工具
            self.context.add_llm_tools(tool_instance)
            
            # 清理残余工具（相反前缀版本）
            if not self.compatibility_config.get("disable_auto_uninstall", False):
                self.context.unregister_llm_tool(legacy_name)
        elif not self.compatibility_config.get("disable_auto_uninstall", False):
            # 工具被禁用时，卸载当前版本和残余版本
            self.context.unregister_llm_tool(current_name)
            self.context.unregister_llm_tool(legacy_name)

    def _manage_browser_tools(self):
        """管理浏览器工具（注册与卸载）"""
        # 原始浏览器工具名称列表
        original_browser_tool_names = [
            "browser_open", "browser_click", "browser_click_xy",
            "browser_click_in_element", "browser_input", "browser_scroll",
            "browser_get_link", "browser_view_image", "browser_screenshot",
            "browser_close", "browser_wait", "browser_send_image", "browser_crop"
        ]
        
        # 计算当前名称列表和残余名称列表
        if self.add_tool_prefix:
            current_names = [f"{self.tool_prefix}{name}" for name in original_browser_tool_names]
            legacy_names = original_browser_tool_names  # 无前缀版本是残余
        else:
            current_names = original_browser_tool_names
            legacy_names = [f"qts_{name}" for name in original_browser_tool_names]  # 带前缀版本是残余

        if self.tool_config.get("browser", False):
            # 自动安装依赖检测（异步执行，不阻塞初始化）
            if self.general_config.get("auto_install_browser_deps", False):
                # 创建异步任务进行依赖安装，安装完成后再注册工具
                asyncio.create_task(self._async_install_browser_deps_and_register(
                    legacy_names
                ))
                logger.info("Browser dependency installation started in background...")
                return  # 异步安装中，稍后注册工具

            # 同步路径：不需要安装依赖，直接注册工具
            self._register_browser_tools(legacy_names)
        elif not self.compatibility_config.get("disable_auto_uninstall", False):
            # 仅当未禁用自动卸载时才执行卸载（当前版本和残余版本都卸载）
            for name in current_names:
                self.context.unregister_llm_tool(name)
            for name in legacy_names:
                self.context.unregister_llm_tool(name)

    async def _async_install_browser_deps_and_register(self, legacy_names: list):
        """异步安装浏览器依赖并注册工具
        
        Args:
            legacy_names: 需要清理的残余工具名称列表
        """
        try:
            installed = await self._ensure_browser_deps_async()
            if installed:
                # 如果进行了安装，需要重新加载相关模块
                try:
                    import importlib
                    from . import browser_core
                    importlib.reload(browser_core)
                    from .tools import browser
                    importlib.reload(browser)
                    logger.info("Browser modules reloaded after dependency installation.")
                except Exception as e:
                    logger.error(f"Failed to reload browser modules: {e}")
            
            # 注册浏览器工具
            self._register_browser_tools(legacy_names)
        except Exception as e:
            logger.error(f"Failed to install browser dependencies: {e}")

    def _register_browser_tools(self, legacy_names: list):
        """注册浏览器工具
        
        Args:
            legacy_names: 需要清理的残余工具名称列表
        """
        # 局部导入，确保获取到的是（可能重载过的）最新类定义
        # 这也解决了在 __init__ 中因条件导入导致的 UnboundLocalError
        from .tools.browser import (
            BrowserOpenTool,
            BrowserClickTool,
            BrowserClickCoordinateTool,
            BrowserClickInElementTool,
            BrowserInputTool,
            BrowserScrollTool,
            BrowserGetLinkTool,
            BrowserViewImageTool,
            BrowserScreenshotTool,
            BrowserCloseTool,
            BrowserWaitTool,
            BrowserSendImageTool,
            BrowserCropTool,
        )

        # 创建所有浏览器工具实例
        browser_tools = [
            BrowserOpenTool(self),
            BrowserClickTool(self),
            BrowserClickCoordinateTool(self),
            BrowserClickInElementTool(self),
            BrowserInputTool(self),
            BrowserScrollTool(self),
            BrowserGetLinkTool(self),
            BrowserViewImageTool(self),
            BrowserScreenshotTool(self),
            BrowserCloseTool(self),
            BrowserWaitTool(self),
            BrowserSendImageTool(self),
            BrowserCropTool(self),
        ]
        
        # 如果启用前缀，修改每个工具的名称
        if self.add_tool_prefix:
            for tool in browser_tools:
                tool.name = f"{self.tool_prefix}{tool.name}"
        
        # 注册所有工具
        for tool in browser_tools:
            self.context.add_llm_tools(tool)
        
        # 清理残余工具（相反前缀版本）
        if not self.compatibility_config.get("disable_auto_uninstall", False):
            for name in legacy_names:
                self.context.unregister_llm_tool(name)
        
        logger.info("Browser tools registered (with click_in_element and crop).")

    def _manage_wake_tools(self):
        """管理唤醒工具（注册与卸载）"""
        original_tool_names = ["schedule", "manage_wake"]
        
        # 计算当前名称和残余名称
        if self.add_tool_prefix:
            current_names = [f"{self.tool_prefix}{name}" for name in original_tool_names]
            legacy_names = original_tool_names
        else:
            current_names = original_tool_names
            legacy_names = [f"qts_{name}" for name in original_tool_names]
        
        if self.tool_config.get("wake_scheduler", True):
            # 初始化唤醒调度器
            # 使用官方 API 获取插件专属数据目录
            data_dir = str(StarTools.get_data_dir())
            
            self.wake_scheduler = WakeScheduler(self.context, data_dir)
            self.wake_scheduler.set_wake_callback(self._wake_callback)
            
            # 启动初始化任务
            asyncio.create_task(self._init_wake_scheduler())
            
            # 创建工具实例
            schedule_tool = WakeScheduleTool(self)
            manage_tool = WakeManageTool(self)
            
            # 如果启用前缀，修改工具名称
            if self.add_tool_prefix:
                schedule_tool.name = f"{self.tool_prefix}{schedule_tool.name}"
                manage_tool.name = f"{self.tool_prefix}{manage_tool.name}"
            
            # 注册工具
            self.context.add_llm_tools(schedule_tool)
            self.context.add_llm_tools(manage_tool)
            
            # 清理残余工具
            if not self.compatibility_config.get("disable_auto_uninstall", False):
                for name in legacy_names:
                    self.context.unregister_llm_tool(name)
            
            logger.info("Wake scheduler tools registered.")
        elif not self.compatibility_config.get("disable_auto_uninstall", False):
            # 工具被禁用时，卸载所有版本
            for name in current_names + legacy_names:
                self.context.unregister_llm_tool(name)
    
    async def _init_wake_scheduler(self):
        """异步初始化唤醒调度器"""
        if self.wake_scheduler:
            await self.wake_scheduler.initialize()
    
    async def _wake_callback(self, task: WakeTask):
        """唤醒回调：创建事件并提交到事件队列，触发 LLM pipeline"""
        try:
            # 解析 session 信息
            # unified_msg_origin 格式: platform_id:message_type:session_id
            parts = task.session_id.split(":")
            if len(parts) != 3:
                logger.error(f"Invalid session_id format: {task.session_id}")
                return
            
            platform_id, message_type_str, session_id = parts
            
            # 获取平台适配器
            platform = self.context.get_platform_inst(platform_id)
            if not platform:
                logger.error(f"Platform not found: {platform_id}")
                return
            
            # 构建唤醒消息内容
            wake_message = "[系统唤醒]"
            if task.remark:
                wake_message += f" 备注: {task.remark}"
            
            # 获取 bot 自身 ID
            bot_self_id = str(platform.client_self_id) if hasattr(platform, 'client_self_id') else "0"
            
            # 判断消息类型并解析正确的 ID
            from astrbot.core.platform.message_type import MessageType as MsgType
            if message_type_str == MsgType.GROUP_MESSAGE.value:
                msg_type = MsgType.GROUP_MESSAGE
                # 对于群消息，session_id 可能是 "user_id_group_id" 或 "group_id"
                if "_" in session_id:
                    # unique_session 模式: user_id_group_id
                    parts_session = session_id.rsplit("_", 1)
                    user_id = parts_session[0]
                    group_id = parts_session[1]
                else:
                    # 普通模式: session_id 就是 group_id
                    group_id = session_id
                    user_id = bot_self_id  # 使用 bot ID 作为发送者
                
                # 群消息的 sender 可以是 bot 自己
                sender_id = bot_self_id
            else:
                msg_type = MsgType.FRIEND_MESSAGE
                group_id = ""
                # 私聊的 session_id 就是用户 ID
                user_id = session_id
                # 私聊时，sender 应该是目标用户（这样 send 时 session_id 就是正确的用户 ID）
                sender_id = user_id
            
            # 创建 AstrBotMessage
            abm = AstrBotMessage()
            abm.self_id = bot_self_id
            abm.sender = MessageMember(user_id=sender_id, nickname="系统唤醒")
            abm.type = msg_type
            abm.group_id = group_id
            abm.session_id = session_id
            abm.message_str = wake_message
            abm.message = [Comp.Plain(wake_message)]
            abm.timestamp = int(time.time())
            abm.message_id = str(uuid.uuid4())
            abm.raw_message = None
            
            # 根据平台类型创建对应的事件
            platform_name = platform.meta().name
            
            if platform_name == "aiocqhttp":
                # aiocqhttp 平台
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                event = AiocqhttpMessageEvent(
                    message_str=wake_message,
                    message_obj=abm,
                    platform_meta=platform.meta(),
                    session_id=session_id,
                    bot=platform.get_client()
                )
            else:
                # 其他平台使用通用方式（可能需要扩展）
                logger.warning(f"Wake callback for platform {platform_name} may not work correctly")
                # 尝试使用 context.send_message 作为降级方案
                from astrbot.core.message.message_event_result import MessageEventResult
                await self.context.send_message(
                    task.session_id,
                    MessageEventResult().message(wake_message)
                )
                return
            
            # 设置唤醒标志
            event.is_wake = True
            event.is_at_or_wake_command = True
            
            # 提交事件到队列
            platform.commit_event(event)
            
            logger.info(f"Wake event committed for session {task.session_id}")
            
        except Exception as e:
            logger.error(f"Failed to execute wake callback: {e}")
            import traceback
            traceback.print_exc()

    async def _ensure_browser_deps_async(self) -> bool:
        """异步检查并自动安装浏览器依赖 (pip install playwright, playwright install chromium)
        
        使用 asyncio.create_subprocess_exec 避免阻塞事件循环。
        
        Returns:
            bool: True 如果进行了安装，False 如果已安装或安装失败
        """
        try:
            import playwright
            return False  # 已安装
        except ImportError:
            pass

        logger.info("检测到未安装 Playwright，正在后台自动安装依赖...")
        import sys

        # 定义重试和镜像策略
        max_retries = 5
        pip_mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        playwright_mirror = "https://npmmirror.com/mirrors/playwright/"
        
        # 1. 安装 playwright 包
        pkg_installed = False
        for i in range(max_retries):
            cmd = [sys.executable, "-m", "pip", "install", "playwright"]
            if i > 0:  # 重试时使用镜像
                cmd.extend(["-i", pip_mirror])
            
            logger.info(f"Installing playwright (Attempt {i+1}/{max_retries})...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                
                if proc.returncode == 0:
                    pkg_installed = True
                    logger.info("Playwright package installed successfully.")
                    break
                else:
                    logger.warning(f"Failed to install playwright (Attempt {i+1}): {stderr.decode()}")
            except Exception as e:
                logger.warning(f"Failed to install playwright (Attempt {i+1}): {e}")
                continue
        
        if not pkg_installed:
            logger.error("Failed to install playwright package after 5 attempts.")
            return False

        # 2. 安装 chromium
        browser_installed = False
        for i in range(max_retries):
            cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
            
            env = os.environ.copy()
            if i > 0:  # 重试时设置镜像环境变量
                env["PLAYWRIGHT_DOWNLOAD_HOST"] = playwright_mirror
                logger.info(f"Installing chromium with mirror (Attempt {i+1}/{max_retries})...")
            else:
                logger.info(f"Installing chromium (Attempt {i+1}/{max_retries})...")
            
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                stdout, stderr = await proc.communicate()
                
                if proc.returncode == 0:
                    browser_installed = True
                    logger.info("Chromium browser installed successfully.")
                    break
                else:
                    logger.warning(f"Failed to install chromium (Attempt {i+1}): {stderr.decode()}")
            except Exception as e:
                logger.warning(f"Failed to install chromium (Attempt {i+1}): {e}")
                continue

        if browser_installed:
            logger.info("Browser dependencies installed successfully.")
            return True
        else:
            logger.error("Failed to install chromium after 5 attempts.")
            return False

    async def terminate(self):
        if hasattr(self, "check_ban_task"):
            self.check_ban_task.cancel()
        
        if hasattr(self, "cache_cleanup_task"):
            self.cache_cleanup_task.cancel()
        
        # 清理唤醒调度器
        if self.wake_scheduler:
            try:
                await self.wake_scheduler.terminate()
                logger.info("Wake scheduler terminated.")
            except Exception as e:
                logger.debug(f"Error terminating wake scheduler: {e}")
        
        # 清理浏览器资源
        try:
            from .browser_core import browser_manager
            await browser_manager.reset()
            logger.info("Browser resources cleaned up.")
        except Exception as e:
            logger.debug(f"Error cleaning up browser: {e}")

    def _get_session_cache(self, session_id: str) -> deque:
        """获取或创建会话缓存，同时更新最后活跃时间
        
        Args:
            session_id: 会话ID
            
        Returns:
            deque: 该会话的消息缓存队列
        """
        current_time = time.time()
        self.cache_last_active[session_id] = current_time
        
        if session_id not in self.message_cache:
            self.message_cache[session_id] = deque(maxlen=self.cache_size)
        
        return self.message_cache[session_id]
    
    async def _cleanup_inactive_caches_loop(self):
        """后台任务：定期清理不活跃的会话缓存
        
        如果 cache_inactive_timeout 为 0 或负数，则禁用自动清理。
        """
        # 如果禁用了自动清理，直接退出
        if self.cache_inactive_timeout <= 0:
            logger.debug("Cache auto-cleanup disabled (cache_inactive_timeout <= 0).")
            return
        
        # 确保清理间隔有效
        cleanup_interval = max(self.cache_cleanup_interval, 60)  # 最小 60 秒
        
        while True:
            try:
                await asyncio.sleep(cleanup_interval)
                await self._cleanup_inactive_caches()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache cleanup loop: {e}")
                await asyncio.sleep(cleanup_interval)
    
    async def _cleanup_inactive_caches(self):
        """清理不活跃的会话缓存
        
        遍历所有缓存的会话，删除超过 cache_inactive_timeout 秒没有活动的会话缓存。
        这可以防止长期运行后不活跃会话占用过多内存。
        
        如果 cache_inactive_timeout <= 0，此方法不会执行任何清理。
        """
        if not self.message_cache:
            return
        
        timeout = self.cache_inactive_timeout
        if timeout <= 0:
            return  # 禁用自动清理
        
        current_time = time.time()
        
        # 找出所有不活跃的会话
        inactive_sessions = [
            sid for sid, last_active in self.cache_last_active.items()
            if current_time - last_active > timeout
        ]
        
        if not inactive_sessions:
            return
        
        # 清理不活跃的会话缓存
        for sid in inactive_sessions:
            if sid in self.message_cache:
                del self.message_cache[sid]
            if sid in self.cache_last_active:
                del self.cache_last_active[sid]
        
        if inactive_sessions:
            logger.debug(f"Cleaned up {len(inactive_sessions)} inactive session caches.")
    
    async def check_ban_expiration(self):
        """定期检查黑名单过期"""
        while True:
            try:
                await asyncio.sleep(5)
                ban_list = self.config.get("ban_list", [])
                if not ban_list:
                    continue

                new_list = []
                changed = False
                
                for ban_info in ban_list:
                    user_id = ban_info.get("user_id")
                    duration = ban_info.get("duration", -1)
                    start_time = ban_info.get("ban_time", 0)
                    
                    if duration != -1 and time.time() > start_time + duration:
                        # Expired
                        logger.info(f"Ban expired for user {user_id}.")
                        changed = True
                    else:
                        new_list.append(ban_info)
                
                if changed:
                    self.config["ban_list"] = new_list
                    self.config.save_config()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in check_ban_expiration: {e}")
                await asyncio.sleep(5)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_all_events(self, event: AstrMessageEvent):
        """监听所有事件，处理 poke notice 缓存和消息缓存"""
        is_notice_event = False
        
        try:
            # 检查是否是 poke notice 事件
            if isinstance(event, AiocqhttpMessageEvent):
                raw_message = getattr(event.message_obj, 'raw_message', None)
                
                # raw_message 可能是 dict 或 Event 对象
                raw_dict = None
                if isinstance(raw_message, dict):
                    raw_dict = raw_message
                elif hasattr(raw_message, '__getitem__'):
                    # Event 对象可能支持 dict-like 访问
                    try:
                        raw_dict = dict(raw_message)
                    except (TypeError, ValueError):
                        pass
                
                if raw_dict:
                    post_type = raw_dict.get('post_type', '')
                    
                    # 检查是否是 notice 事件
                    if post_type == 'notice':
                        is_notice_event = True
                        
                        # 检查是否是 poke notice
                        if (raw_dict.get('notice_type') == 'notify' and
                            raw_dict.get('sub_type') == 'poke'):
                            # 缓存 poke notice 事件
                            poke_info = {
                                'timestamp': time.time(),
                                'user_id': raw_dict.get('user_id'),  # 发起者
                                'target_id': raw_dict.get('target_id'),  # 被戳者
                                'group_id': raw_dict.get('group_id'),  # 群号（私聊时无）
                                'raw_info': raw_dict.get('raw_info'),  # 动作文案信息
                                'raw_message': raw_dict.get('raw_message'),  # 兼容旧字段
                                'raw_event': raw_dict,  # 保留完整事件
                            }
                            self.poke_notice_cache.append(poke_info)
                            logger.debug(f"Cached poke notice: user_id={poke_info['user_id']}, target_id={poke_info['target_id']}, raw_info={poke_info['raw_info']}")
        except Exception as e:
            logger.debug(f"Error processing poke notice: {e}")
        
        # 仅对消息事件执行消息处理逻辑（notice 事件跳过）
        if not is_notice_event:
            await self._on_message_internal(event)

    async def _on_message_internal(self, event: AstrMessageEvent):
        """监听所有消息并缓存，同时处理忙碌会话的消息排队
        
        priority=100 确保此处理器在 LongTermMemory 之前执行，
        这样 LTM 记录的群聊历史中也会包含 [MSG_ID:xxx] 和文件信息。
        """
        try:
            # 引用消息增强：
            # AstrBot 的 OneBot(V11) 适配器默认只把「文本段」拼进 message_str，
            # 因此被引用消息如果只有图片/文件/卡片等非文本内容，Reply.message_str 会是空，
            # 进而在日志与上下文里只能看到 [引用消息]。
            if self.general_config.get("enhance_reply_quote", True):
                self._enhance_reply_quote(event)

            # Check Ban
            sender_id = event.get_sender_id()
            ban_list = self.config.get("ban_list", [])
            for ban_info in ban_list:
                if ban_info.get("user_id") == sender_id:
                    # Check expiration (double check)
                    duration = ban_info.get("duration", -1)
                    start_time = ban_info.get("ban_time", 0)
                    if duration != -1 and time.time() > start_time + duration:
                        continue # Let background task handle removal
                    
                    event.stop_event()
                    return

            # 获取 session_id (群号或私聊用户ID)
            session_id = event.get_session_id()
            if not session_id:
                return

            # 1. 获取消息ID
            message_id = event.message_obj.message_id
            
            # 2. 提取文件信息并添加到消息中
            file_info_parts = []
            if self.general_config.get("show_file_info", False):
                file_info_parts = self._extract_file_info(event)
            
            # 3. 如果有文件信息，追加到 message_str 和 message chain
            if file_info_parts:
                file_info_str = " " + " ".join(file_info_parts)
                
                # 防止重复添加（检查第一个文件标记是否已存在）
                if file_info_parts[0] not in event.message_str:
                    event.message_str += file_info_str
                    
                    # 同步 event.message_obj.message_str
                    if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                        event.message_obj.message_str += file_info_str
                    
                    # 追加到 message chain
                    if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
                        event.message_obj.message.append(Comp.Plain(file_info_str))
            
            # 4. 如果配置启用，提取并注入 B站卡片上下文
            if self.general_config.get("inject_bilibili_card_context", True):
                bili_blocks = self._extract_bilibili_card_blocks(event)
                if bili_blocks:
                    # 去重：检查是否已存在相同的 [BILI_CARD ...] 行
                    existing_str = event.message_str or ""
                    new_blocks = [b for b in bili_blocks if b not in existing_str]
                    
                    if new_blocks:
                        bili_suffix = "\n" + "\n".join(new_blocks)
                        
                        # 追加到 message_str
                        event.message_str += bili_suffix
                        
                        # 同步 event.message_obj.message_str
                        if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                            event.message_obj.message_str += bili_suffix
                        
                        # 追加到 message chain
                        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
                            event.message_obj.message.append(Comp.Plain(bili_suffix))
            
            # 5. 处理 MSG_ID 追加逻辑
            # - show_message_id: 控制是否在消息中显示 MSG_ID
            # - delay_append_msg_id: 如果启用，不将 MSG_ID 注入到 event（避免污染 LTM），
            #                        但仍在缓存中保留 MSG_ID 供工具使用
            show_msg_id = self.general_config.get("show_message_id", True)
            delay_msg_id = self.compatibility_config.get("delay_append_msg_id", False)
            
            id_suffix = f" [MSG_ID:{message_id}]" if show_msg_id else ""
            
            # 只有在 show_message_id 启用且 delay_append_msg_id 未启用时，才注入到 event
            if show_msg_id and not delay_msg_id:
                # 防止重复添加
                if id_suffix not in event.message_str:
                    event.message_str += id_suffix
                
                # 同步 event.message_obj.message_str（某些地方会读取这个属性）
                if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                    if id_suffix not in event.message_obj.message_str:
                        event.message_obj.message_str += id_suffix
                
                # 追加 ID 到 message chain (Plain Text)，确保多模态下也能看到
                # 注意：某些 Adapter 可能没有 .message 属性或者结构不同，需防御性编程
                if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
                    event.message_obj.message.append(Comp.Plain(id_suffix))

            # 提取消息基本信息
            # 注意：缓存中的 content 始终包含 MSG_ID（如果 show_message_id 启用），
            # 即使 delay_append_msg_id 启用（不注入到 event），工具仍能从缓存获取 MSG_ID
            cache_content = event.message_str
            if show_msg_id and delay_msg_id:
                # delay 模式：event 中没有 MSG_ID，但缓存中要有
                if id_suffix not in cache_content:
                    cache_content += id_suffix
            
            # 优先使用消息真实时间戳（从 raw_message 中获取）
            # OneBot 事件中的 time 字段是消息的真实发送时间
            # 如果获取失败，回退到 message_obj.timestamp，最后使用当前时间
            real_timestamp = self._get_real_message_timestamp(event)
            
            msg_info = {
                "message_id": message_id,
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
                "content": cache_content,  # 缓存内容始终包含 MSG_ID（供工具使用）
                "timestamp": real_timestamp,
                "raw_message": event.message_obj.raw_message  # 保存原始消息对象以备不时之需
            }
            
            # 存入缓存（使用 _get_session_cache 确保更新活跃时间）
            self._get_session_cache(session_id).append(msg_info)
            
        except Exception as e:
            logger.error(f"Error processing message in QQToolsPlugin: {e}")

    def _get_real_message_timestamp(self, event: AstrMessageEvent) -> int:
        """获取消息的真实时间戳
        
        优先级：
        1. raw_message 中的 time 字段（OneBot 事件的真实消息时间）
        2. message_obj.timestamp（AstrBot 设置的时间戳，通常也是收到时间）
        3. 当前时间（兜底）
        
        Args:
            event: 消息事件
            
        Returns:
            int: Unix 时间戳（秒）
        """
        try:
            # 尝试从 raw_message 获取真实时间戳
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if raw_message is not None:
                # raw_message 可能是 Event 对象或 dict
                raw_time = None
                
                # 尝试作为 dict 访问
                if isinstance(raw_message, dict):
                    raw_time = raw_message.get('time')
                elif hasattr(raw_message, 'time'):
                    # Event 对象通常有 time 属性
                    raw_time = raw_message.time
                elif hasattr(raw_message, '__getitem__'):
                    # 支持 dict-like 访问
                    try:
                        raw_time = raw_message['time']
                    except (KeyError, TypeError):
                        pass
                
                if raw_time is not None:
                    # 确保是整数
                    ts = int(raw_time)
                    # 基本合理性检查：时间戳应该是正数且不超过未来 1 年
                    if 0 < ts < int(time.time()) + 31536000:
                        return ts
            
            # 尝试使用 message_obj.timestamp
            msg_timestamp = getattr(event.message_obj, 'timestamp', None)
            if msg_timestamp is not None:
                ts = int(msg_timestamp)
                if 0 < ts < int(time.time()) + 31536000:
                    return ts
        except Exception as e:
            logger.debug(f"Error getting real message timestamp: {e}")
        
        # 兜底：使用当前时间
        return int(time.time())

    def _extract_file_info(self, event: AstrMessageEvent) -> list:
        """从消息中提取文件信息，返回格式化的文件信息列表
        
        格式: [File:name=xxx,type=video,id=xxx,size=xxx]
        """
        file_info_parts = []
        
        # 获取消息组件列表
        messages = event.get_messages() if hasattr(event, 'get_messages') else []
        if not messages and hasattr(event.message_obj, 'message'):
            messages = event.message_obj.message
        
        # 获取原始消息数据以提取更多信息
        raw_message = getattr(event.message_obj, 'raw_message', None)
        raw_segments = []
        if raw_message and hasattr(raw_message, 'message') and isinstance(raw_message.message, list):
            raw_segments = raw_message.message
        
        # 创建一个从组件类型到原始数据的映射
        raw_data_by_type = {}
        for seg in raw_segments:
            if isinstance(seg, dict) and 'type' in seg:
                seg_type = seg['type']
                if seg_type not in raw_data_by_type:
                    raw_data_by_type[seg_type] = []
                raw_data_by_type[seg_type].append(seg.get('data', {}))
        
        # 用于跟踪每种类型处理的索引
        type_indices = {}
        
        for comp in messages:
            info_parts = []
            comp_type = None
            
            if isinstance(comp, Comp.File):
                comp_type = 'file'
                # 文件类型
                info_parts.append(f"name={comp.name or 'unknown'}")
                info_parts.append("type=file")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('file', 0)
                type_indices['file'] = idx + 1
                if 'file' in raw_data_by_type and idx < len(raw_data_by_type['file']):
                    raw_data = raw_data_by_type['file'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Video):
                comp_type = 'video'
                # 视频类型
                file_name = getattr(comp, 'path', '') or getattr(comp, 'file', '') or 'video'
                if '/' in file_name:
                    file_name = file_name.split('/')[-1]
                if '\\' in file_name:
                    file_name = file_name.split('\\')[-1]
                info_parts.append(f"name={file_name}")
                info_parts.append("type=video")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('video', 0)
                type_indices['video'] = idx + 1
                if 'video' in raw_data_by_type and idx < len(raw_data_by_type['video']):
                    raw_data = raw_data_by_type['video'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    elif 'file' in raw_data:
                        # 有时候 file 字段包含 ID
                        file_val = raw_data['file']
                        if file_val and not file_val.startswith('http'):
                            info_parts.append(f"id={file_val[:16]}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Record):
                comp_type = 'record'
                # 音频类型
                file_name = getattr(comp, 'path', '') or getattr(comp, 'file', '') or 'audio'
                if '/' in file_name:
                    file_name = file_name.split('/')[-1]
                if '\\' in file_name:
                    file_name = file_name.split('\\')[-1]
                info_parts.append(f"name={file_name}")
                info_parts.append("type=audio")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('record', 0)
                type_indices['record'] = idx + 1
                if 'record' in raw_data_by_type and idx < len(raw_data_by_type['record']):
                    raw_data = raw_data_by_type['record'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    elif 'file' in raw_data:
                        file_val = raw_data['file']
                        if file_val and not file_val.startswith('http'):
                            info_parts.append(f"id={file_val[:16]}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Image):
                comp_type = 'image'
                # 图片类型 - 可选，因为图片通常已经有 [Image] 标记
                if self.general_config.get("show_image_as_file", False):
                    file_name = getattr(comp, 'file_unique', '') or 'image'
                    info_parts.append(f"name={file_name}")
                    info_parts.append("type=image")
                    
                    idx = type_indices.get('image', 0)
                    type_indices['image'] = idx + 1
                    if 'image' in raw_data_by_type and idx < len(raw_data_by_type['image']):
                        raw_data = raw_data_by_type['image'][idx]
                        if 'file_id' in raw_data:
                            info_parts.append(f"id={raw_data['file_id']}")
                        elif 'file' in raw_data:
                            file_val = raw_data['file']
                            if file_val and not file_val.startswith('http') and not file_val.startswith('base64'):
                                info_parts.append(f"id={file_val[:16]}")
                        if 'file_size' in raw_data:
                            info_parts.append(f"size={raw_data['file_size']}")
            
            # 如果有信息要添加
            if info_parts:
                file_info_parts.append(f"[File:{','.join(info_parts)}]")
        
        return file_info_parts

    # -----------------------------
    # Reply / Quote enhancement
    # -----------------------------
    def _outline_component_for_quote(self, comp: object) -> str:
        """将消息段转换为适合放进引用消息概要的短文本。"""
        try:
            if isinstance(comp, Comp.Plain):
                text = (comp.text or "").strip()
                return re.sub(r"\s+", " ", text)
            if isinstance(comp, Comp.Image):
                return "图片"
            if hasattr(Comp, "Video") and isinstance(comp, Comp.Video):
                return "视频"
            if hasattr(Comp, "Record") and isinstance(comp, Comp.Record):
                return "语音"
            if hasattr(Comp, "File") and isinstance(comp, Comp.File):
                name = getattr(comp, "name", "") or ""
                name = str(name).strip()
                return f"文件:{name}" if name else "文件"
            if hasattr(Comp, "Json") and isinstance(comp, Comp.Json):
                return "卡片"
            if hasattr(Comp, "Forward") and isinstance(comp, Comp.Forward):
                return "转发消息"
            if hasattr(Comp, "Nodes") and isinstance(comp, Comp.Nodes):
                return "转发消息"
            if hasattr(Comp, "Node") and isinstance(comp, Comp.Node):
                return "转发消息"
            if hasattr(Comp, "Face") and isinstance(comp, Comp.Face):
                fid = getattr(comp, "id", "")
                return f"表情:{fid}" if fid else "表情"
            if hasattr(Comp, "At") and isinstance(comp, Comp.At):
                # 不要太长，优先显示 name
                name = getattr(comp, "name", "") or ""
                qq = getattr(comp, "qq", "") or ""
                name = str(name).strip()
                qq = str(qq).strip()
                if name and qq and qq != "all":
                    return f"@{name}({qq})"
                if name:
                    return f"@{name}"
                if qq:
                    return "@全体成员" if qq == "all" else f"@{qq}"
                return "@"
        except Exception:
            pass

        # 兜底
        t = getattr(comp, "type", None)
        if isinstance(t, str) and t:
            return t
        return comp.__class__.__name__

    def _build_quote_summary_from_chain(self, chain: Optional[list]) -> str:
        """从被引用消息链生成概要文本（兼容图片/文件/卡片等非文本）。"""
        if not chain:
            return ""

        parts: list[str] = []
        for seg in chain:
            s = self._outline_component_for_quote(seg)
            if not s:
                continue
            parts.append(s)

        # 去掉重复的空格并拼接
        summary = " ".join(p for p in parts if p)
        summary = re.sub(r"\s+", " ", summary).strip()

        max_len = int(self.general_config.get("reply_quote_max_len", 80))
        if max_len > 0 and len(summary) > max_len:
            summary = summary[: max_len - 1].rstrip() + "…"
        return summary

    def _enhance_reply_quote(self, event: AstrMessageEvent):
        """增强 Reply 段的 message_str，使日志/上下文能显示图片/文件等被引用内容。"""
        # 仅对 aiocqhttp 平台生效（其他平台的 Reply 结构可能不同）
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        msgs = []
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            msgs = event.message_obj.message
        elif hasattr(event, "get_messages"):
            msgs = event.get_messages()

        if not msgs:
            return

        include_msg_id = bool(self.general_config.get("reply_quote_include_msg_id", True))
        inject_into_message_str = bool(self.general_config.get("inject_reply_quote_into_message_str", True))
        enrich_even_if_text = bool(self.general_config.get("reply_quote_enrich_even_if_text", True))

        quote_prefixes: list[str] = []

        for seg in msgs:
            if not isinstance(seg, Comp.Reply):
                continue

            chain = getattr(seg, "chain", None)
            summary = self._build_quote_summary_from_chain(chain)

            # 如果 chain 无法构造摘要，回退到现有 message_str
            existing = (getattr(seg, "message_str", "") or "").strip()
            if not summary:
                summary = existing

            # chain 能构造摘要时，可选择覆盖/增强已有文本（比如“文字+图片”）
            if summary and (enrich_even_if_text or not existing):
                seg.message_str = summary
                # 兼容字段
                if hasattr(seg, "text"):
                    seg.text = summary

            # 需要在摘要里补充被引用消息的 msg_id
            if include_msg_id:
                mid = str(getattr(seg, "id", "") or "").strip()
                if mid and mid not in summary:
                    # 放在末尾更接近“内容摘要 + id”的阅读习惯
                    summary_with_id = f"{summary} MSG_ID:{mid}" if summary else f"MSG_ID:{mid}"
                    seg.message_str = summary_with_id
                    if hasattr(seg, "text"):
                        seg.text = summary_with_id
                    summary = summary_with_id

            # 生成可注入 message_str 的前缀（让 LLM 也能看到引用内容）
            if inject_into_message_str:
                nickname = (getattr(seg, "sender_nickname", "") or "").strip() or "N/A"
                if summary:
                    quote_prefixes.append(f"[引用消息({nickname}: {summary})]")
                else:
                    quote_prefixes.append("[引用消息]")

        if inject_into_message_str and quote_prefixes:
            prefix = " ".join(quote_prefixes).strip() + " "

            # 防止重复注入
            if not (event.message_str or "").startswith(prefix.strip()):
                event.message_str = prefix + (event.message_str or "")

            # 同步 message_obj.message_str
            if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                if not event.message_obj.message_str.startswith(prefix.strip()):
                    event.message_obj.message_str = prefix + event.message_obj.message_str

    def _extract_bilibili_card_blocks(self, event: AstrMessageEvent) -> List[str]:
        """从消息中提取 B站分享卡片信息，返回格式化的 [BILI_CARD ...] 块列表
        
        只处理 type == "json" 或 type == "xml" 的消息段，
        只有确认是 B站卡片（命中 BV/av/bilibili URL/b23.tv）时才生成输出。
        非 B站卡片一律忽略，不生成任何内容。
        
        Returns:
            list[str]: [BILI_CARD ref=<BV...|av...> p=<1> url=<...> source=<json|xml>] 格式的字符串列表
        """
        blocks = []
        
        try:
            # 获取原始消息数据
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if not raw_message:
                return blocks
            
            # 获取消息段列表
            raw_segments = []
            if hasattr(raw_message, 'message') and isinstance(raw_message.message, list):
                raw_segments = raw_message.message
            elif isinstance(raw_message, dict) and 'message' in raw_message:
                raw_segments = raw_message.get('message', [])
            
            if not raw_segments:
                return blocks
            
            # 遍历消息段，查找 json/xml 类型
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                
                seg_type = seg.get('type', '')
                if seg_type not in ('json', 'xml'):
                    continue
                
                # 获取 payload（data 字段中的 data 或直接的 data）
                seg_data = seg.get('data', {})
                payload = seg_data.get('data', '') if isinstance(seg_data, dict) else str(seg_data)
                
                if not payload:
                    continue
                
                # 尝试解析并提取 B站信息
                bili_info = self._parse_bilibili_from_payload(payload, seg_type)
                if bili_info:
                    block = self._format_bili_card_block(bili_info, seg_type)
                    if block and block not in blocks:  # 去重
                        blocks.append(block)
        
        except Exception as e:
            logger.debug(f"Error extracting bilibili card blocks: {e}")
        
        return blocks

    def _parse_bilibili_from_payload(self, payload: str, source_type: str) -> Optional[Dict]:
        """从 payload 字符串中解析 B站信息
        
        Args:
            payload: json/xml 消息的原始字符串内容
            source_type: 'json' 或 'xml'
            
        Returns:
            dict: {'bvid': ..., 'aid': ..., 'url': ..., 'p': ...} 或 None（非B站）
        """
        if not payload:
            return None
        
        # 收集所有需要扫描的字符串
        strings_to_scan = [payload]
        
        # 如果是 JSON，尝试深层解析
        if payload.strip().startswith('{') or payload.strip().startswith('['):
            try:
                json_obj = json.loads(payload)
                strings_to_scan.extend(self._collect_json_strings(json_obj))
            except (json.JSONDecodeError, ValueError):
                pass  # 解析失败，仍然用原始字符串扫描
        
        # 合并所有字符串进行扫描
        combined_text = ' '.join(strings_to_scan)
        
        # 正则模式
        bv_pattern = r"(BV[0-9A-Za-z]{10})"
        av_pattern = r"(?:av)(\d+)"
        url_patterns = [
            r"(https?://(?:www\.|m\.)?bilibili\.com/video/[^\s\"'<>]+)",
            r"(https?://b23\.tv/[^\s\"'<>]+)"
        ]
        p_pattern = r"[?&]p=(\d+)"
        
        bvid = None
        aid = None
        url = None
        p = 1
        
        # 提取 BV 号
        bv_match = re.search(bv_pattern, combined_text, re.I)
        if bv_match:
            bvid = bv_match.group(1)
            # 标准化 BV 号格式（保持大小写）
            if bvid.lower().startswith('bv'):
                bvid = 'BV' + bvid[2:]
        
        # 提取 av 号
        av_match = re.search(av_pattern, combined_text, re.I)
        if av_match:
            aid = av_match.group(1)
        
        # 提取 URL
        for url_pat in url_patterns:
            url_match = re.search(url_pat, combined_text)
            if url_match:
                url = url_match.group(1)
                break
        
        # 从 URL 中提取 p 参数
        if url:
            p_match = re.search(p_pattern, url)
            if p_match:
                try:
                    p = int(p_match.group(1))
                except ValueError:
                    p = 1
        
        # 判断是否为 B站卡片：必须命中 BV/av/bilibili URL/b23.tv 之一
        is_bilibili = bool(bvid or aid or url)
        
        if not is_bilibili:
            return None
        
        return {
            'bvid': bvid,
            'aid': aid,
            'url': url,
            'p': p
        }

    def _collect_json_strings(self, obj, depth: int = 0) -> List[str]:
        """递归收集 JSON 对象中的所有字符串值
        
        Args:
            obj: JSON 对象（dict/list/str/其他）
            depth: 当前递归深度（防止过深递归）
            
        Returns:
            list[str]: 所有字符串值的列表
        """
        strings = []
        
        if depth > 20:  # 防止过深递归
            return strings
        
        if isinstance(obj, str):
            strings.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                strings.extend(self._collect_json_strings(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                strings.extend(self._collect_json_strings(item, depth + 1))
        
        return strings

    def _format_bili_card_block(self, info: Dict, source_type: str) -> Optional[str]:
        """格式化 B站卡片信息为标准输出格式
        
        Args:
            info: {'bvid': ..., 'aid': ..., 'url': ..., 'p': ...}
            source_type: 'json' 或 'xml'
            
        Returns:
            str: [BILI_CARD ref=<BV...|av...> p=<1> url=<...> source=<json|xml>]
        """
        if not info:
            return None
        
        bvid = info.get('bvid')
        aid = info.get('aid')
        url = info.get('url', '')
        p = info.get('p', 1)
        
        # ref 优先 BV，其次 av
        if bvid:
            ref = bvid
        elif aid:
            ref = f"av{aid}"
        else:
            ref = ""
        
        # 构建输出
        parts = []
        if ref:
            parts.append(f"ref={ref}")
        parts.append(f"p={p}")
        if url:
            parts.append(f"url={url}")
        parts.append(f"source={source_type}")
        
        return f"[BILI_CARD {' '.join(parts)}]"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        在消息发送前进行处理，将 [At:123456] 转换为真实的 At 消息组件。
        同时支持消息内容过滤。
        """
        # 仅针对 QQ 平台 (Aiocqhttp)
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        enable_at_conversion = self.general_config.get("enable_auto_at_conversion", False)
        msg_filter_patterns = self.general_config.get("message_filter_patterns", [])

        if not enable_at_conversion and not msg_filter_patterns:
            return

        new_chain = []
        for component in result.chain:
            if isinstance(component, Comp.Plain) and component.text:
                current_text = component.text

                # 0. 消息内容过滤 (Regex)
                if msg_filter_patterns:
                    for pattern in msg_filter_patterns:
                        try:
                            current_text = re.sub(pattern, "", current_text)
                        except Exception:
                            pass
                
                if not current_text:
                    continue

                # 1. 尝试解析泄露的工具调用
                is_leaked_tool = False
                if self.compatibility_config.get("fix_tool_leak", True):
                    filter_patterns = self.compatibility_config.get("filter_patterns", ["&&.*?&&"])
                    content, message_id = parse_leaked_tool_call(current_text, filter_patterns=filter_patterns)
                    
                    if content is not None and message_id is not None:
                        # 解析成功，构造 Reply 和 Content
                        logger.warning(f"Detected leaked tool call in text. Fixing... ID: {message_id}, Content: {content}")
                        new_chain.append(Comp.Reply(id=message_id))
                        # 对提取出的内容再进行 At 解析
                        if enable_at_conversion:
                            new_chain.extend(parse_at_content(content))
                        else:
                            new_chain.append(Comp.Plain(content))
                        is_leaked_tool = True

                if not is_leaked_tool:
                    # 2. 常规解析 At
                    if enable_at_conversion:
                        new_chain.extend(parse_at_content(current_text))
                    else:
                        new_chain.append(Comp.Plain(current_text))
            else:
                new_chain.append(component)
        
        result.chain = new_chain

        if not new_chain:
            event.stop_event()
            logger.info("Message chain is empty after filtering. Event stopped.")

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后，获取并缓存 BOT 发送的消息
        
        这样可以确保 get_recent_messages 工具能够查询到 BOT 自己发送的消息 ID
        """
        try:
            # 检查是否启用了缓存 BOT 消息功能
            if not self.general_config.get("cache_bot_messages", True):
                return
                
            # 仅针对 QQ 平台 (Aiocqhttp)
            if not isinstance(event, AiocqhttpMessageEvent):
                return
                
            session_id = event.get_session_id()
            if not session_id:
                return
                
            # 调用 API 获取最新消息并缓存 BOT 发送的消息
            await self._cache_bot_sent_messages(event)
            
        except Exception as e:
            logger.error(f"Error in on_after_message_sent: {e}")

    async def _cache_bot_sent_messages(self, event: AiocqhttpMessageEvent):
        """获取最近的历史消息，缓存 BOT 发送的消息"""
        client = event.bot
        session_id = event.get_session_id()
        self_id = str(event.get_self_id())
        
        try:
            api_history_count = self.general_config.get("api_history_count", 10)
            
            if event.get_group_id():
                # 群聊：获取群历史消息
                group_id = int(event.get_group_id())
                resp = await call_onebot(
                    client,
                    'get_group_msg_history',
                    group_id=group_id,
                    count=api_history_count
                )
            else:
                # 私聊：获取好友历史消息
                user_id = int(event.get_sender_id())
                resp = await call_onebot(
                    client,
                    'get_friend_msg_history',
                    user_id=user_id,
                    count=api_history_count
                )
            
            if not resp or 'messages' not in resp:
                return
                
            messages = resp.get('messages', [])
            
            for msg in messages:
                sender_id = str(msg.get('sender', {}).get('user_id', ''))
                
                # 只缓存 BOT 自己发送的消息
                if sender_id != self_id:
                    continue
                    
                message_id = str(msg.get('message_id', ''))
                
                # 检查是否已在缓存中
                if self._is_message_cached(session_id, message_id):
                    continue
                    
                # 构建消息信息
                msg_info = self._build_msg_info_from_api(msg, self_id)
                
                # 存入缓存（使用 _get_session_cache 确保更新活跃时间）
                self._get_session_cache(session_id).append(msg_info)
                logger.debug(f"Cached BOT message: {message_id}")
                
        except Exception as e:
            logger.warning(f"Failed to cache BOT messages: {e}")

    def _is_message_cached(self, session_id: str, message_id: str) -> bool:
        """检查消息是否已在缓存中
        
        注意：此方法只检查不修改，不会更新活跃时间
        """
        if session_id not in self.message_cache:
            return False
        for msg in self.message_cache.get(session_id, []):
            if str(msg.get('message_id', '')) == str(message_id):
                return True
        return False

    def _build_msg_info_from_api(self, msg: dict, self_id: str) -> dict:
        """从 API 响应构建消息信息"""
        sender = msg.get('sender', {})
        sender_id = str(sender.get('user_id', ''))
        sender_name = sender.get('card', '') or sender.get('nickname', '') or 'Unknown'
        
        # 如果是 BOT 自己的消息，标记发送者名称
        if sender_id == self_id:
            sender_name = f"[BOT]{sender_name}"
        
        # 提取消息内容
        content = self._extract_message_content(msg.get('message', []))
        message_id = str(msg.get('message_id', ''))
        
        # 添加 MSG_ID 标记
        if self.general_config.get("show_message_id", True):
            content += f" [MSG_ID:{message_id}]"
        
        return {
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
            "timestamp": msg.get('time', int(time.time())),
            "raw_message": msg,
            "is_bot_message": sender_id == self_id  # 标记是否为 BOT 消息
        }

    def _extract_message_content(self, message_segments: list) -> str:
        """从消息段列表提取文本内容"""
        parts = []
        for seg in message_segments:
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                data = seg.get('data', {})
            else:
                # 可能是其他类型的消息段对象
                continue
            
            if seg_type == 'text':
                parts.append(data.get('text', ''))
            elif seg_type == 'image':
                parts.append('[图片]')
            elif seg_type == 'at':
                qq = data.get('qq', '')
                parts.append(f'@{qq}')
            elif seg_type == 'face':
                parts.append('[表情]')
            elif seg_type == 'record':
                parts.append('[语音]')
            elif seg_type == 'video':
                parts.append('[视频]')
            elif seg_type == 'file':
                parts.append(f"[文件:{data.get('name', 'file')}]")
            elif seg_type == 'reply':
                parts.append(f"[回复:{data.get('id', '')}]")
            else:
                parts.append(f'[{seg_type}]')
        
        return ''.join(parts)

    async def fetch_history_from_api(self, event: AstrMessageEvent, count: int = 50) -> list:
        """从 Napcat API 获取历史消息（供工具调用使用）
        
        Args:
            event: 消息事件
            count: 获取的消息数量
            
        Returns:
            消息信息列表
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            return []
        
        client = event.bot
        self_id = str(event.get_self_id())
        
        try:
            if event.get_group_id():
                group_id = int(event.get_group_id())
                resp = await call_onebot(
                    client,
                    'get_group_msg_history',
                    group_id=group_id,
                    count=count
                )
            else:
                user_id = int(event.get_sender_id())
                resp = await call_onebot(
                    client,
                    'get_friend_msg_history',
                    user_id=user_id,
                    count=count
                )
            
            if not resp or 'messages' not in resp:
                return []
            
            messages = []
            for msg in resp.get('messages', []):
                msg_info = self._build_msg_info_from_api(msg, self_id)
                messages.append(msg_info)
            
            return messages
            
        except Exception as e:
            logger.warning(f"Failed to fetch history from API: {e}")
            return []

    # =============================================
    # 管理员命令：唤醒任务管理 (qts wk)
    # =============================================
    
    @filter.command_group("qts wk")
    def qts_wk(self):
        """唤醒任务管理命令组"""
        pass
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @qts_wk.command("list")
    async def qts_wk_list(self, event: AstrMessageEvent, scope: str = ""):
        """列出唤醒任务
        
        Args:
            scope(string): 范围，可选 "all" 列出所有会话，留空则只列出当前会话
        """
        if not self.wake_scheduler:
            yield event.plain_result("唤醒调度器未启用。")
            return
        
        if scope.lower() == "all":
            # 列出所有会话的任务
            tasks = self.wake_scheduler.list_tasks()
            if not tasks:
                yield event.plain_result("没有任何待触发的唤醒任务。")
                return
            
            lines = [f"📅 所有唤醒任务列表 ({len(tasks)} 个)：\n"]
            for i, task in enumerate(tasks, 1):
                lines.append(f"{i}. {task.format_display()}")
                lines.append(f"   会话: {task.session_id}")
            
            yield event.plain_result("\n".join(lines))
        else:
            # 列出当前会话的任务
            session_id = event.unified_msg_origin
            tasks = self.wake_scheduler.list_tasks(session_id=session_id)
            
            if not tasks:
                yield event.plain_result("当前会话没有待触发的唤醒任务。")
                return
            
            lines = [f"📅 当前会话唤醒任务列表 ({len(tasks)} 个)：\n"]
            for i, task in enumerate(tasks, 1):
                lines.append(f"{i}. {task.format_display()}")
            
            yield event.plain_result("\n".join(lines))
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @qts_wk.command("del")
    async def qts_wk_del(self, event: AstrMessageEvent, task_id: str = ""):
        """删除指定的唤醒任务
        
        Args:
            task_id(string): 要删除的任务ID
        """
        if not self.wake_scheduler:
            yield event.plain_result("唤醒调度器未启用。")
            return
        
        if not task_id:
            yield event.plain_result("请指定要删除的任务ID。\n用法：/qts wk del <task_id>")
            return
        
        # 管理员可以删除任何会话的任务（不传 session_id）
        success = await self.wake_scheduler.delete_task(task_id=task_id)
        
        if success:
            yield event.plain_result(f"✅ 已删除唤醒任务: {task_id[:8]}...")
        else:
            yield event.plain_result(f"❌ 未找到任务: {task_id[:8]}...")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @qts_wk.command("clear")
    async def qts_wk_clear(self, event: AstrMessageEvent, scope: str = ""):
        """清空唤醒任务
        
        Args:
            scope(string): 范围，可选 "all" 清空所有会话，留空则只清空当前会话
        """
        if not self.wake_scheduler:
            yield event.plain_result("唤醒调度器未启用。")
            return
        
        if scope.lower() == "all":
            # 清空所有会话的任务
            count = await self.wake_scheduler.clear_tasks()
            if count == 0:
                yield event.plain_result("没有任何待清空的唤醒任务。")
            else:
                yield event.plain_result(f"✅ 已清空所有会话的 {count} 个唤醒任务。")
        else:
            # 清空当前会话的任务
            session_id = event.unified_msg_origin
            count = await self.wake_scheduler.clear_tasks(session_id=session_id)
            
            if count == 0:
                yield event.plain_result("当前会话没有待清空的唤醒任务。")
            else:
                yield event.plain_result(f"✅ 已清空当前会话的 {count} 个唤醒任务。")
