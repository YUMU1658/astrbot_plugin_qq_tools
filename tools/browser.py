"""
Browser Tools - 浏览器相关工具

提供网页浏览功能的 LLM 工具集。

重要改进（根据研究报告）：
1. 截图使用 scale='css' 确保坐标系一致
2. 注入截图时替换旧图而非追加，减少上下文堆积
3. 扩充标记脚本，支持 Canvas/SVG/[onclick]/[tabindex]/[role] 等元素
4. 新增 browser_click_in_element 和 browser_crop 工具
"""

import aiohttp
import asyncio
import base64
from typing import List, Optional, Tuple

from astrbot.api import logger
from astrbot.api import message_components as Comp
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.agent.message import ImageURLPart, TextPart

from ..browser_core import browser_manager
from ..utils import check_tool_permission, get_original_tool_name


async def _check_browser_tool_permission(plugin, tool_name: str, event) -> Tuple[bool, Optional[str]]:
    """检查浏览器工具权限的辅助函数
    
    Args:
        plugin: 插件实例
        tool_name: 工具名称
        event: 消息事件
        
    Returns:
        Tuple[bool, Optional[str]]: (是否有权限, 拒绝原因或None)
    """
    if not plugin:
        return True, None
    
    permission_config = plugin.config.get("tool_permission", {})
    original_name = get_original_tool_name(tool_name, plugin.add_tool_prefix)
    
    client = getattr(event, 'bot', None)
    return await check_tool_permission(
        original_name,
        event,
        permission_config,
        client
    )


def _replace_image_in_content(content: list, image_id: str, new_part: ImageURLPart) -> list:
    """替换消息内容中的指定 ID 图片，而非追加
    
    这样可以避免上下文中堆积多张旧截图，减少 token 消耗和模型注意力分散。
    
    Args:
        content: 消息内容列表
        image_id: 要替换的图片 ID
        new_part: 新的图片组件
        
    Returns:
        替换后的内容列表
    """
    cleaned = []
    for p in content:
        # 跳过具有相同 ID 的旧图片
        if isinstance(p, ImageURLPart):
            if hasattr(p, 'image_url') and hasattr(p.image_url, 'id'):
                if p.image_url.id == image_id:
                    continue
        cleaned.append(p)
    cleaned.append(new_part)
    return cleaned


async def inject_browser_image(
    context: ContextWrapper[AstrAgentContext],
    image_bytes: bytes,
    info: str,
    image_id: str = "browser_screenshot",
    success_suffix: str = "页面截图已更新到你的视觉上下文中。"
) -> str:
    """将图片注入到 LLM 上下文中（替换旧图片而非追加）
    
    这是一个共享的截图/图片注入函数，用于减少代码重复。
    
    Args:
        context: 上下文包装器
        image_bytes: 图片的二进制数据 (PNG格式)
        info: 操作结果信息
        image_id: 图片标识符，用于替换旧图片。默认 "browser_screenshot"
        success_suffix: 成功时附加的提示信息
        
    Returns:
        操作结果字符串
    """
    try:
        # 将图片转换为 base64 data URL
        base64_data = base64.b64encode(image_bytes).decode('utf-8')
        data_url = f"data:image/png;base64,{base64_data}"
        
        # 获取会话历史
        messages = context.messages
        
        if messages:
            # 构造图片组件
            img_part = ImageURLPart(
                image_url=ImageURLPart.ImageURL(
                    url=data_url,
                    id=image_id
                )
            )
            
            # 查找最近的 User 消息，替换旧图片（而非追加）
            for msg in reversed(messages):
                if msg.role == "user":
                    if isinstance(msg.content, str):
                        msg.content = [TextPart(text=msg.content)]
                    
                    if isinstance(msg.content, list):
                        # 使用替换逻辑，移除旧的同 ID 图片
                        msg.content = _replace_image_in_content(msg.content, image_id, img_part)
                        logger.info(f"Image injected to LLM context with id='{image_id}' (replaced old one).")
                        break
        
        return f"✅ {info}\n\n{success_suffix}"
        
    except Exception as e:
        logger.error(f"Failed to inject image (id={image_id}): {e}")
        return f"✅ {info}\n\n⚠️ 图片注入失败: {e}"


class BrowserOpenTool(FunctionTool):
    """打开网页工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_open",
            description=(
                "打开指定的网页URL，返回带有元素标记的页面截图。\n"
                "页面元素标记说明：\n"
                "- 🟢 绿色 [数字] 标记：可输入元素（输入框、文本域等），可使用 browser_input 工具输入文本\n"
                "- 🔴 红色 数字 标记：可点击元素（链接、按钮、图片等），使用 browser_click 工具点击\n"
                "- 🔵 蓝色 <数字> 标记：Canvas/SVG 元素（地图、游戏、图表等），使用 browser_click_in_element 工具在元素内相对位置点击\n\n"
                "注意：截图会加载到你的视觉上下文供你分析，但不会自动发送给用户。如需发送截图给用户，请使用 browser_screenshot 工具。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要打开的网页URL，如 https://www.example.com",
                    },
                },
                "required": ["url"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        url = kwargs.get("url")
        if not url:
            return "❌ 缺少参数：url"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 配置浏览器
        config = self.plugin.config.get("browser_config", {})
        browser_manager.configure(
            timeout_seconds=config.get("timeout_seconds", 180),
            viewport_width=config.get("viewport_width", 1280),
            viewport_height=config.get("viewport_height", 720),
            # 标签渲染配置
            mark_mode=config.get("mark_mode", "balanced"),
            max_marks=config.get("max_marks", 80),
            min_element_area=config.get("min_element_area", 400),
            nms_iou_threshold=config.get("nms_iou_threshold", 0.6),
            # 安全配置 - SSRF 防护
            allow_private_network=config.get("allow_private_network", False),
            allowed_domains=config.get("allowed_domains", []),
            blocked_domains=config.get("blocked_domains", [])
        )
        
        # 打开网页
        screenshot, info = await browser_manager.navigate(url)
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 将截图注入到上下文中（使用详细的工具说明作为成功提示）
        detailed_suffix = (
            "系统提示：页面截图已加载到你的视觉上下文中（仅供你分析，用户看不到）。\n\n"
            "📌 元素标记说明：\n"
            "- 🟢 绿色 [数字] 标记：可输入元素，使用 browser_input 输入文本\n"
            "- 🔴 红色 数字 标记：可点击元素，使用 browser_click 点击\n"
            "- 🔵 蓝色 <数字> 标记：Canvas/SVG元素，使用 browser_click_in_element 在元素内点击\n\n"
            "📌 可用工具：\n"
            "- browser_click: 点击指定ID的红色标记元素\n"
            "- browser_input: 在指定ID的绿色标记元素中输入文本（仅限绿色 [ID] 标记）\n"
            "- browser_click_in_element: 在蓝色 <ID> 标记的 Canvas/SVG 元素内相对位置点击\n"
            "- browser_click_xy: 兜底工具，点击指定坐标 (x, y)\n"
            "- browser_crop: 裁剪放大指定区域，用于精确定位\n"
            "- browser_scroll: 滚动页面 (up/down/top/bottom)\n"
            "- browser_get_link: 获取指定ID元素的链接或文本\n"
            "- browser_view_image: 查看指定ID图片的原始内容\n"
            "- browser_screenshot: 将当前页面截图发送给用户\n"
            "- browser_close: 关闭浏览器释放控制权"
        )
        return await inject_browser_image(context, screenshot, info, success_suffix=detailed_suffix)


class BrowserClickTool(FunctionTool):
    """点击元素工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_click",
            description="点击页面上指定ID的元素（链接、按钮等）。点击后会返回新的页面截图。此工具支持跨 Frame 点击。",
            parameters={
                "type": "object",
                "properties": {
                    "element_id": {
                        "type": "integer",
                        "description": "要点击的元素ID（页面截图中红色标记的数字）",
                    },
                },
                "required": ["element_id"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        element_id = kwargs.get("element_id")
        if element_id is None:
            return "❌ 缺少参数：element_id"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 点击元素
        screenshot, info = await browser_manager.click_element(int(element_id))
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入截图（使用共享函数）
        return await inject_browser_image(context, screenshot, info)


class BrowserClickCoordinateTool(FunctionTool):
    """点击坐标工具 (兜底)"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_click_xy",
            description=(
                "这是一个兜底工具。当且仅当截图中找不到红色数字 ID 时（如地图、验证码、Canvas 游戏），使用此工具。"
                "注意：坐标 (x, y) 必须基于当前的浏览器分辨率。请勿假设固定分辨率，请根据截图比例和系统提示中的分辨率数值估算坐标。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "点击位置的 X 坐标",
                    },
                    "y": {
                        "type": "integer",
                        "description": "点击位置的 Y 坐标",
                    },
                },
                "required": ["x", "y"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        x = kwargs.get("x")
        y = kwargs.get("y")
        
        if x is None or y is None:
            return "❌ 缺少参数：x 或 y"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 点击坐标
        screenshot, info = await browser_manager.click_coordinates(int(x), int(y))
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入截图（使用共享函数）
        return await inject_browser_image(context, screenshot, info)


class BrowserInputTool(FunctionTool):
    """输入文本工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_input",
            description=(
                "在页面上指定ID的输入框中输入文本，或直接在当前焦点输入文本。\n"
                "⚠️ 重要：只能对绿色 [数字] 标记的元素使用此工具！\n"
                "- 绿色 [ID] 标记 = 可输入元素（输入框、文本域等）→ 使用此工具\n"
                "- 红色 ID 标记 = 可点击元素（按钮、链接等）→ 请使用 browser_click\n\n"
                "如果提供了 element_id，会在指定元素中输入。\n"
                "如果未提供 element_id，会直接在当前页面焦点位置输入（适用于已点击输入框后的场景）。\n"
                "输入后会返回新的页面截图。此工具支持跨 Frame 输入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "element_id": {
                        "type": "integer",
                        "description": "输入框的元素ID（页面截图中绿色 [数字] 标记的数字）。如果不提供，将直接在当前焦点输入。",
                    },
                    "text": {
                        "type": "string",
                        "description": "要输入的文本内容",
                    },
                },
                "required": ["text"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        element_id = kwargs.get("element_id")
        text = kwargs.get("text")
        
        if not text:
            return "❌ 缺少参数：text"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        if element_id is not None:
            # 输入文本到指定元素
            screenshot, info = await browser_manager.input_text(int(element_id), text)
        else:
            # 直接输入文本到当前焦点
            screenshot, info = await browser_manager.type_text(text)
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入截图（使用共享函数）
        return await inject_browser_image(context, screenshot, info)


class BrowserScrollTool(FunctionTool):
    """滚动页面工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_scroll",
            description="滚动页面。滚动后会返回新的页面截图。",
            parameters={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "滚动方向：up（向上一屏）、down（向下一屏）、top（滚动到顶部）、bottom（滚动到底部）",
                        "enum": ["up", "down", "top", "bottom"],
                    },
                },
                "required": ["direction"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        direction = kwargs.get("direction")
        if not direction:
            return "❌ 缺少参数：direction"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 滚动页面
        screenshot, info = await browser_manager.scroll(direction)
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入截图（使用共享函数）
        return await inject_browser_image(context, screenshot, info)


class BrowserGetLinkTool(FunctionTool):
    """获取元素链接/文本工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_get_link",
            description="获取指定ID元素的详细信息，包括链接地址、文本内容、图片地址等。支持跨 Frame 元素。",
            parameters={
                "type": "object",
                "properties": {
                    "element_id": {
                        "type": "integer",
                        "description": "元素ID（页面截图中红色标记的数字）",
                    },
                },
                "required": ["element_id"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        element_id = kwargs.get("element_id")
        if element_id is None:
            return "❌ 缺少参数：element_id"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 获取元素信息
        info, desc = await browser_manager.get_element_info(int(element_id))
        
        if info is None:
            return f"❌ {desc}"
        
        return f"✅ 元素 {element_id} 的信息：\n{desc}"


class BrowserViewImageTool(FunctionTool):
    """查看图片工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_view_image",
            description="获取页面上指定ID图片元素的原始图片。这会返回干净的图片（不含标记），并将其加载到你的视觉上下文中。支持跨 Frame 元素。",
            parameters={
                "type": "object",
                "properties": {
                    "element_id": {
                        "type": "integer",
                        "description": "图片元素的ID（页面截图中红色标记的数字）",
                    },
                },
                "required": ["element_id"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        element_id = kwargs.get("element_id")
        if element_id is None:
            return "❌ 缺少参数：element_id"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 获取元素截图
        screenshot, info = await browser_manager.screenshot_element(int(element_id))
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入图片到上下文（使用共享函数，自定义 image_id）
        return await inject_browser_image(
            context, screenshot, info,
            image_id="browser_element_image",
            success_suffix="系统提示：图片已加载到你的视觉上下文中，你可以直接描述看到的内容。"
        )


class BrowserScreenshotTool(FunctionTool):
    """发送截图给用户工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_screenshot",
            description="将当前浏览器页面的截图发送给用户。当用户希望看到网页内容时使用此工具。调用此工具后，截图会自动发送给用户。",
            parameters={
                "type": "object",
                "properties": {
                    "clean": {
                        "type": "boolean",
                        "description": "是否发送干净的截图（不含元素标记）。默认 false，会包含红色数字标记。",
                    },
                },
                "required": [],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        clean = kwargs.get("clean", False)
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 检查浏览器是否已打开页面
        if not browser_manager.page:
            return "❌ 浏览器未打开任何页面。请先使用 browser_open 打开网页。"
        
        try:
            if clean:
                # 隐藏所有 Frame 的标记后截图
                for frame in browser_manager.page.frames:
                    try:
                        if not frame.is_detached():
                            await frame.evaluate("""
                                () => {
                                    document.querySelectorAll('.ai-mark').forEach(e => e.style.display = 'none');
                                }
                            """)
                    except:
                        pass

                # 使用 scale='css' 确保坐标系一致
                try:
                    screenshot = await browser_manager.page.screenshot(type='png', scale='css')
                except TypeError:
                    # 兼容旧版 playwright
                    screenshot = await browser_manager.page.screenshot(type='png')
                
                # 恢复标记
                for frame in browser_manager.page.frames:
                    try:
                        if not frame.is_detached():
                            await frame.evaluate("""
                                () => {
                                    document.querySelectorAll('.ai-mark').forEach(e => e.style.display = '');
                                }
                            """)
                    except:
                        pass
            else:
                # 确保标记存在并截图
                screenshot, _ = await browser_manager.get_marked_screenshot()
            
            if screenshot is None:
                return "❌ 截图失败"
            
            # 获取页面信息
            page_info = await browser_manager.get_page_info()
            title = page_info.get("title", "未知页面")
            url = page_info.get("url", "")
            
            # 构造消息链：图片
            chain = [
                Comp.Image.fromBytes(screenshot)
            ]
            
            # 直接发送图片给用户
            await event.send(event.chain_result(chain))
            
            logger.info(f"Browser screenshot sent to user: {title}")
            
            return f"✅ 截图已发送给用户。\n📸 页面: {title}\n🔗 {url}"
            
        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return f"❌ 截图失败: {e}"


class BrowserCloseTool(FunctionTool):
    """关闭浏览器工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_close",
            description="关闭浏览器并释放控制权。完成网页浏览后应调用此工具。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 释放权限
        success, msg = await browser_manager.release_permission(user_id)
        
        if success:
            return f"✅ {msg}"
        else:
            return f"❌ {msg}"


class BrowserWaitTool(FunctionTool):
    """等待页面加载工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_wait",
            description="等待指定的秒数，让页面有时间加载动态内容。当页面包含AJAX加载的内容、懒加载图片、或需要等待动画/渲染完成时使用此工具。等待结束后会返回更新的页面截图。",
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "等待的秒数，范围1-30秒。建议：简单动态内容用2-3秒，复杂页面用5-10秒。",
                        "minimum": 1,
                        "maximum": 30,
                    },
                },
                "required": ["seconds"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        seconds = kwargs.get("seconds")
        
        if seconds is None:
            return "❌ 缺少参数：seconds"
        
        # 限制范围
        seconds = max(1, min(30, int(seconds)))
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 检查浏览器是否已打开页面
        if not browser_manager.page:
            return "❌ 浏览器未打开任何页面。请先使用 browser_open 打开网页。"
        
        try:
            # 等待指定秒数
            await asyncio.sleep(seconds)
            
            # 尝试等待网络空闲（最多再等2秒）
            try:
                await browser_manager.page.wait_for_load_state('networkidle', timeout=2000)
            except:
                pass  # 超时不影响，继续执行
            
            # 获取更新的截图
            screenshot, info = await browser_manager.get_marked_screenshot()
            
            if screenshot is None:
                return f"✅ 已等待 {seconds} 秒。\n⚠️ 截图获取失败: {info}"
            
            # 注入截图到上下文（使用共享函数）
            return await inject_browser_image(context, screenshot, f"已等待 {seconds} 秒，页面内容已更新。{info}")
            
        except Exception as e:
            logger.error(f"Error during wait: {e}")
            return f"❌ 等待过程中出错: {e}"


class BrowserSendImageTool(FunctionTool):
    """发送图片给用户工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_send_image",
            description="发送图片给用户。可以通过图片URL直接发送，或通过页面上的元素ID获取图片并发送。支持同时发送多张图片。当用户想要保存或查看网页上的图片时使用此工具。",
            parameters={
                "type": "object",
                "properties": {
                    "image_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "图片URL列表。直接提供图片的网络地址，如 ['https://example.com/image1.jpg', 'https://example.com/image2.png']",
                    },
                    "element_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "页面上图片元素的ID列表（页面截图中红色标记的数字）。会从这些元素的src属性获取图片URL并发送。如 [1, 3, 5]",
                    },
                },
                "required": [],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        image_urls = kwargs.get("image_urls", []) or []
        element_ids = kwargs.get("element_ids", []) or []
        
        # 确保至少有一个参数
        if not image_urls and not element_ids:
            return "❌ 请至少提供 image_urls 或 element_ids 参数之一。"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 收集所有需要发送的图片URL
        all_image_urls: List[str] = list(image_urls)
        element_results: List[str] = []
        
        # 如果提供了元素ID，从页面获取图片URL
        if element_ids:
            # 检查浏览器权限
            has_permission, msg = await browser_manager.acquire_permission(user_id)
            if not has_permission:
                return f"❌ {msg}"
            
            # 检查浏览器是否已打开页面
            if not browser_manager.page:
                return "❌ 浏览器未打开任何页面。请先使用 browser_open 打开网页，或直接提供 image_urls 参数。"
            
            # 从元素获取图片URL
            for element_id in element_ids:
                try:
                    url, info = await self._get_image_url_from_element(int(element_id))
                    if url:
                        all_image_urls.append(url)
                        element_results.append(f"元素 {element_id}: ✅ 获取成功")
                    else:
                        element_results.append(f"元素 {element_id}: ❌ {info}")
                except Exception as e:
                    element_results.append(f"元素 {element_id}: ❌ 获取失败 - {e}")
        
        if not all_image_urls:
            element_info = "\n".join(element_results) if element_results else ""
            return f"❌ 未能获取到任何有效的图片URL。\n{element_info}"
        
        # 下载并发送图片
        success_count = 0
        fail_count = 0
        results: List[str] = []
        
        for i, url in enumerate(all_image_urls):
            try:
                image_bytes = await self._download_image(url)
                if image_bytes:
                    # 发送单张图片
                    chain = [Comp.Image.fromBytes(image_bytes)]
                    await event.send(event.chain_result(chain))
                    success_count += 1
                    results.append(f"图片 {i+1}: ✅ 发送成功")
                    logger.info(f"Image sent successfully: {url[:50]}...")
                else:
                    fail_count += 1
                    results.append(f"图片 {i+1}: ❌ 下载失败")
            except Exception as e:
                fail_count += 1
                results.append(f"图片 {i+1}: ❌ {e}")
                logger.error(f"Failed to send image {url}: {e}")
        
        # 构建返回信息
        summary = f"✅ 图片发送完成：成功 {success_count} 张"
        if fail_count > 0:
            summary += f"，失败 {fail_count} 张"
        
        detail_info = ""
        if element_results:
            detail_info += "\n\n📋 元素获取结果：\n" + "\n".join(element_results)
        
        if len(results) > 1 or fail_count > 0:
            detail_info += "\n\n📤 发送结果：\n" + "\n".join(results)
        
        return summary + detail_info
    
    async def _get_image_url_from_element(self, element_id: int) -> Tuple[Optional[str], str]:
        """从页面元素获取图片URL
        
        Args:
            element_id: 元素ID (data-ai-id)
            
        Returns:
            Tuple[Optional[str], str]: (图片URL, 状态信息)
        """
        if not browser_manager.page:
            return None, "浏览器未初始化"
        
        try:
            target_element = None
            target_frame = None
            
            # 遍历所有 Frames 查找元素
            for frame in browser_manager.page.frames:
                try:
                    if frame.is_detached():
                        continue
                    element = await frame.query_selector(f'[data-ai-id="{element_id}"]')
                    if element:
                        target_element = element
                        target_frame = frame
                        break
                except Exception:
                    continue
            
            if not target_element:
                return None, f"未找到 ID 为 {element_id} 的元素。"

            # 获取元素的图片URL（支持img的src、背景图片等）
            result = await target_frame.evaluate(f"""
                () => {{
                    const el = document.querySelector('[data-ai-id="{element_id}"]');
                    if (!el) return {{ error: '未找到元素' }};
                    
                    // 如果是 img 标签，获取 src
                    if (el.tagName.toLowerCase() === 'img') {{
                        return {{ url: el.src || el.getAttribute('src') }};
                    }}
                    
                    // 如果是 video 标签，获取 poster
                    if (el.tagName.toLowerCase() === 'video') {{
                        const poster = el.poster || el.getAttribute('poster');
                        if (poster) return {{ url: poster }};
                        return {{ error: '视频元素没有封面图' }};
                    }}
                    
                    // 如果是 picture/source 标签
                    if (el.tagName.toLowerCase() === 'source') {{
                        return {{ url: el.srcset || el.getAttribute('srcset') }};
                    }}
                    
                    // 检查是否有背景图片
                    const style = window.getComputedStyle(el);
                    const bgImage = style.backgroundImage;
                    if (bgImage && bgImage !== 'none') {{
                        const match = bgImage.match(/url\\(["']?(.+?)["']?\\)/);
                        if (match) return {{ url: match[1] }};
                    }}
                    
                    // 检查是否有 data-src (懒加载图片)
                    const dataSrc = el.getAttribute('data-src') || el.getAttribute('data-original');
                    if (dataSrc) return {{ url: dataSrc }};
                    
                    // 检查子元素中是否有 img
                    const childImg = el.querySelector('img');
                    if (childImg) {{
                        return {{ url: childImg.src || childImg.getAttribute('src') }};
                    }}
                    
                    return {{ error: '该元素不是图片或不包含图片' }};
                }}
            """)
            
            if result.get('error'):
                return None, result['error']
            
            url = result.get('url')
            if not url:
                return None, "未能获取图片URL"
            
            # 处理相对URL
            if url.startswith('//'):
                url = 'https:' + url
            elif url.startswith('/'):
                # 获取当前页面的origin
                origin = await target_frame.evaluate("window.location.origin")
                url = origin + url
            
            return url, "获取成功"
            
        except Exception as e:
            logger.error(f"Failed to get image URL from element {element_id}: {e}")
            return None, f"获取失败: {e}"
    
    async def _download_image(self, url: str, timeout: int = 30) -> Optional[bytes]:
        """下载图片"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                    'Referer': browser_manager.page.url if browser_manager.page else '',
                }
                
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to download image: HTTP {resp.status} - {url}")
                        return None
                    
                    # 检查内容类型
                    content_type = resp.headers.get('Content-Type', '')
                    if not content_type.startswith('image/'):
                        logger.warning(f"Not an image content type: {content_type} - {url}")
                        # 仍然尝试返回内容，因为有些服务器可能返回错误的Content-Type
                    
                    # 检查文件大小（限制50MB）
                    content_length = resp.headers.get('Content-Length')
                    if content_length and int(content_length) > 50 * 1024 * 1024:
                        logger.warning(f"Image too large: {content_length} bytes - {url}")
                        return None
                    
                    return await resp.read()
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout downloading image: {url}")
            return None
        except Exception as e:
            logger.error(f"Error downloading image {url}: {e}")
            return None


class BrowserClickInElementTool(FunctionTool):
    """在元素内相对位置点击工具（用于 Canvas/SVG/地图等）"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_click_in_element",
            description=(
                "在指定ID元素内的相对位置点击。专为 Canvas、SVG、地图、游戏等无法标记内部元素的场景设计。\n\n"
                "使用方法：\n"
                "1. 在截图中找到蓝色 <数字> 标记的 Canvas/SVG 元素\n"
                "2. 估计目标位置在元素内的相对坐标（0~1 范围）\n"
                "   - rx=0 表示最左边，rx=1 表示最右边\n"
                "   - ry=0 表示最上边，ry=1 表示最下边\n"
                "   - 例如：点击元素中心用 (0.5, 0.5)，点击右下角用 (0.9, 0.9)\n\n"
                "提示：如果需要更精确定位，可以先使用 browser_crop 裁剪放大目标区域。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "element_id": {
                        "type": "integer",
                        "description": "Canvas/SVG 元素的 ID（页面截图中蓝色 <数字> 标记的数字）",
                    },
                    "rx": {
                        "type": "number",
                        "description": "相对 X 坐标（0.0~1.0），0 表示最左，1 表示最右",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "ry": {
                        "type": "number",
                        "description": "相对 Y 坐标（0.0~1.0），0 表示最上，1 表示最下",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["element_id", "rx", "ry"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        element_id = kwargs.get("element_id")
        rx = kwargs.get("rx")
        ry = kwargs.get("ry")
        
        if element_id is None:
            return "❌ 缺少参数：element_id"
        if rx is None or ry is None:
            return "❌ 缺少参数：rx 或 ry（相对坐标）"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 在元素内点击
        screenshot, info = await browser_manager.click_in_element(int(element_id), float(rx), float(ry))
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入截图（使用共享函数）
        return await inject_browser_image(context, screenshot, info)


class BrowserCropTool(FunctionTool):
    """裁剪放大区域工具"""
    
    def __init__(self, plugin_instance):
        super().__init__(
            name="browser_crop",
            description=(
                "裁剪并放大页面指定区域的截图，用于精确定位小按钮、验证码、Canvas细节等。\n\n"
                "使用场景：\n"
                "- 坐标点击前需要更精确地定位目标\n"
                "- 需要看清小元素或文字\n"
                "- Canvas/地图中需要精确点击某个位置\n\n"
                "裁剪后会返回放大的区域图片。注意：裁剪区域内的坐标从 (0,0) 开始，对应原图的 (x, y) 位置。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "裁剪区域左上角 X 坐标",
                    },
                    "y": {
                        "type": "integer",
                        "description": "裁剪区域左上角 Y 坐标",
                    },
                    "width": {
                        "type": "integer",
                        "description": "裁剪区域宽度（像素）",
                    },
                    "height": {
                        "type": "integer",
                        "description": "裁剪区域高度（像素）",
                    },
                    "scale": {
                        "type": "number",
                        "description": "放大倍数（1.0~4.0），默认 2.0",
                        "minimum": 1,
                        "maximum": 4,
                    },
                },
                "required": ["x", "y", "width", "height"],
            }
        )
        self.plugin = plugin_instance
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        x = kwargs.get("x")
        y = kwargs.get("y")
        width = kwargs.get("width")
        height = kwargs.get("height")
        scale = kwargs.get("scale", 2.0)
        
        if x is None or y is None or width is None or height is None:
            return "❌ 缺少参数：x, y, width, height"
        
        event = context.context.event
        user_id = event.get_sender_id()
        
        # 工具权限检查
        has_permission, reason = await _check_browser_tool_permission(self.plugin, self.name, event)
        if not has_permission:
            return reason
        
        # 检查浏览器会话权限
        has_permission, msg = await browser_manager.acquire_permission(user_id)
        if not has_permission:
            return f"❌ {msg}"
        
        # 检查浏览器是否已打开页面
        if not browser_manager.page:
            return "❌ 浏览器未打开任何页面。请先使用 browser_open 打开网页。"
        
        # 裁剪截图
        screenshot, info = await browser_manager.crop_screenshot(
            int(x), int(y), int(width), int(height), float(scale)
        )
        
        if screenshot is None:
            return f"❌ {info}"
        
        # 注入裁剪图到上下文（使用共享函数，自定义 image_id）
        return await inject_browser_image(
            context, screenshot, info,
            image_id="browser_crop_image",
            success_suffix="系统提示：裁剪放大后的图片已加载到你的视觉上下文中。"
        )