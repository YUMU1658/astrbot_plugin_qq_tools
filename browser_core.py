"""
Browser Core Module - 浏览器核心管理模块

使用 Playwright + Visual Grounding (视觉标记) 方案实现网页浏览功能。

安全特性：
- URL 验证防止 SSRF 攻击
- 支持私有网络访问控制
- 支持域名白名单/黑名单
"""

import asyncio
import time
from typing import Optional, Tuple, Dict, Any, List

from astrbot.api import logger

from .url_validator import URLValidator, validate_browser_url

# Playwright 导入会在实际使用时进行
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright, Frame
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed. Browser features will be disabled.")


class BrowserManager:
    """浏览器管理器单例类
    
    提供浏览器操作的核心功能，包括：
    - 懒加载浏览器实例
    - 并发锁控制（同一时间只允许一个用户操作）
    - 页面截图与元素标记（支持跨 Frame）
    - 页面交互（点击、输入、滚动等）
    """
    
    _instance: Optional["BrowserManager"] = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        
        # Playwright 相关对象
        self.playwright: Optional["Playwright"] = None
        self.browser: Optional["Browser"] = None
        self.context: Optional["BrowserContext"] = None
        self.page: Optional["Page"] = None
        
        # 并发控制
        self.lock = asyncio.Lock()
        self.current_user: Optional[str] = None
        self.last_active_time: float = 0.0
        
        # 配置
        self.timeout_seconds: float = 180.0  # 默认 3 分钟超时
        self.viewport_width: int = 1280
        self.viewport_height: int = 720
        
        # 记录 context 创建时使用的 viewport 配置（用于检测配置变化）
        self._context_viewport_width: Optional[int] = None
        self._context_viewport_height: Optional[int] = None
        
        # 标签渲染配置
        self.mark_mode: str = "balanced"  # "minimal" | "balanced" | "all"
        self.max_marks: int = 80  # 最大标记数量
        self.min_element_area: int = 400  # 最小元素面积 (20x20)
        self.nms_iou_threshold: float = 0.6  # NMS IoU 阈值
        
        # 安全配置 - SSRF 防护
        self.allow_private_network: bool = False  # 默认禁止访问私有网络
        self.allowed_domains: List[str] = []  # 域名白名单
        self.blocked_domains: List[str] = []  # 域名黑名单
        self._url_validator: Optional[URLValidator] = None
    
    def configure(
        self,
        timeout_seconds: float = 180.0,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        mark_mode: str = "balanced",
        max_marks: int = 80,
        min_element_area: int = 400,
        nms_iou_threshold: float = 0.6,
        # 安全配置
        allow_private_network: bool = False,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None
    ):
        """配置浏览器参数
        
        Args:
            timeout_seconds: 会话超时时间（秒）
            viewport_width: 视口宽度
            viewport_height: 视口高度
            mark_mode: 标签模式 - "minimal"(最少), "balanced"(平衡), "all"(全部)
            max_marks: 最大标记数量
            min_element_area: 最小元素面积（像素²）
            nms_iou_threshold: NMS 重叠抑制阈值 (0-1)
            allow_private_network: 是否允许访问私有网络（默认 False，禁止 SSRF）
            allowed_domains: 域名白名单（可选，支持通配符如 *.example.com）
            blocked_domains: 域名黑名单（可选）
        """
        self.timeout_seconds = timeout_seconds
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.mark_mode = mark_mode
        self.max_marks = max_marks
        self.min_element_area = min_element_area
        self.nms_iou_threshold = nms_iou_threshold
        
        # 安全配置
        self.allow_private_network = allow_private_network
        self.allowed_domains = allowed_domains or []
        self.blocked_domains = blocked_domains or []
        
        # 重建 URL 验证器
        self._url_validator = URLValidator(
            allow_private_network=self.allow_private_network,
            allowed_domains=self.allowed_domains,
            blocked_domains=self.blocked_domains
        )
        
        # 记录安全配置状态
        if allow_private_network:
            logger.warning("Browser security: Private network access is ENABLED. SSRF protection is reduced.")
        else:
            logger.info("Browser security: Private network access is DISABLED (default safe mode).")
        
        if self.allowed_domains:
            logger.info(f"Browser security: Domain whitelist enabled with {len(self.allowed_domains)} entries.")
        if self.blocked_domains:
            logger.info(f"Browser security: Domain blacklist enabled with {len(self.blocked_domains)} entries.")
    
    async def _init_browser(self) -> bool:
        """懒加载启动 Playwright 浏览器
        
        Returns:
            bool: 是否成功启动
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright is not available. Please install it first.")
            return False
        
        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            
            if self.browser is None:
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-accelerated-2d-canvas',
                        '--no-first-run',
                        '--no-zygote',
                        '--disable-gpu'
                    ]
                )
            
            # 如果 Context 存在但视口大小不匹配，需要重建 Context
            if self.context:
                # 检查当前视口配置是否与 context 创建时的配置一致
                viewport_changed = (
                    self._context_viewport_width != self.viewport_width or
                    self._context_viewport_height != self.viewport_height
                )
                
                if viewport_changed:
                    logger.info(
                        f"Viewport configuration changed from "
                        f"{self._context_viewport_width}x{self._context_viewport_height} to "
                        f"{self.viewport_width}x{self.viewport_height}. Rebuilding context..."
                    )
                    # Playwright context 视口无法动态修改，需要关闭并重建
                    try:
                        if self.page:
                            await self.page.close()
                            self.page = None
                    except Exception as e:
                        logger.debug(f"Error closing page during viewport rebuild: {e}")
                    
                    try:
                        await self.context.close()
                        self.context = None
                    except Exception as e:
                        logger.debug(f"Error closing context during viewport rebuild: {e}")
                    
                    # 清除记录的 viewport 配置
                    self._context_viewport_width = None
                    self._context_viewport_height = None

            if self.context is None:
                self.context = await self.browser.new_context(
                    viewport={'width': self.viewport_width, 'height': self.viewport_height},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                # 记录 context 创建时使用的 viewport 配置
                self._context_viewport_width = self.viewport_width
                self._context_viewport_height = self.viewport_height
            
            if self.page is None:
                self.page = await self.context.new_page()
            
            logger.info("Browser initialized successfully.")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            await self.reset()
            return False
    
    async def reset(self):
        """关闭所有资源并重置状态"""
        try:
            if self.page:
                await self.page.close()
        except Exception as e:
            logger.debug(f"Error closing page: {e}")
        
        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            logger.debug(f"Error closing context: {e}")
        
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.debug(f"Error stopping playwright: {e}")
        
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.current_user = None
        self.last_active_time = 0.0
        
        # 清除记录的 viewport 配置
        self._context_viewport_width = None
        self._context_viewport_height = None
        
        logger.info("Browser resources reset.")
    
    async def acquire_permission(self, user_id: str) -> Tuple[bool, str]:
        """获取浏览器操作权限"""
        current_time = time.time()
        
        async with self.lock:
            # 检查是否超时
            if self.current_user and current_time - self.last_active_time > self.timeout_seconds:
                logger.info(f"Browser session timed out for user {self.current_user}. Resetting...")
                await self.reset()
            
            # 如果当前没有用户占用
            if self.current_user is None:
                self.current_user = user_id
                self.last_active_time = current_time
                return True, "已获取浏览器控制权。"
            
            # 如果是同一个用户
            if self.current_user == user_id:
                self.last_active_time = current_time
                return True, "继续操作。"
            
            # 如果是其他用户
            remaining_time = int(self.timeout_seconds - (current_time - self.last_active_time))
            return False, f"浏览器当前被用户 {self.current_user} 占用中，请等待 {remaining_time} 秒后重试，或等待对方释放。"
    
    async def release_permission(self, user_id: str) -> Tuple[bool, str]:
        """释放浏览器操作权限"""
        async with self.lock:
            if self.current_user == user_id:
                await self.reset()
                return True, "已释放浏览器控制权。"
            elif self.current_user is None:
                return True, "浏览器当前没有被占用。"
            else:
                return False, f"无法释放：浏览器当前被用户 {self.current_user} 占用。"
    
    def _get_mark_script(self, start_id: int) -> str:
        """获取元素标记的 JavaScript 脚本
        
        Args:
            start_id: 起始 ID
            
        优化版标记脚本，使用语义优先 + 打分 + NMS去重 + Top-N截断：
        - 语义优先：优先收集强交互元素（button, a, input 等）
        - 打分机制：根据元素类型、语义信息给予不同分数
        - NMS去重：基于 IoU 重叠抑制，避免父子元素重复标记
        - Top-N截断：限制最大标记数量，避免"满屏数字"
        """
        return f"""
        () => {{
            const startId = {start_id};
            const maxMarks = {self.max_marks};
            const minArea = {self.min_element_area};
            const iouThreshold = {self.nms_iou_threshold};
            const markMode = '{self.mark_mode}';
            
            // 清理旧标记 (仅清理当前 Frame 的标记)
            document.querySelectorAll('.ai-mark').forEach(e => e.remove());
            
            // 清理旧的元素标识，避免重复 data-ai-id 导致点击错位/点错
            document.querySelectorAll('[data-ai-id]').forEach(el => {{
                el.removeAttribute('data-ai-id');
                el.removeAttribute('data-ai-inputable');
                el.removeAttribute('data-ai-canvas');
            }});

            // ========== 1. 收集候选元素（语义优先）==========
            const candidateSet = new Set();
            
            // 强交互元素选择器（最高优先级）
            const strongSelectors = [
                'a[href]',
                'button:not([disabled])',
                'input:not([type="hidden"]):not([disabled])',
                'textarea:not([disabled])',
                'select:not([disabled])',
                '[role="button"]',
                '[role="link"]',
                '[role="textbox"]',
                '[role="checkbox"]',
                '[role="radio"]',
                '[role="switch"]',
                '[role="menuitem"]',
                '[role="tab"]',
                '[role="option"]',
                '[role="slider"]',
                '[role="spinbutton"]',
                '[role="combobox"]',
                '[contenteditable="true"]'
            ];
            
            // 事件驱动元素选择器
            const eventSelectors = [
                '[onclick]',
                '[onmousedown]',
                '[onmouseup]',
                '[tabindex]:not([tabindex="-1"])'
            ];
            
            // 特殊元素选择器
            const specialSelectors = ['canvas', 'svg', 'video', 'audio', 'img'];
            
            // 收集强交互元素
            strongSelectors.forEach(sel => {{
                try {{
                    document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
                }} catch(e) {{}}
            }});
            
            // 收集事件驱动元素
            eventSelectors.forEach(sel => {{
                try {{
                    document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
                }} catch(e) {{}}
            }});
            
            // 收集特殊元素
            specialSelectors.forEach(sel => {{
                try {{
                    document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
                }} catch(e) {{}}
            }});
            
            // 在 balanced 和 all 模式下，收集 pointer 元素（需满足附加条件）
            if (markMode !== 'minimal') {{
                const allElements = document.querySelectorAll('*');
                allElements.forEach(el => {{
                    if (candidateSet.has(el)) return;
                    
                    try {{
                        const style = window.getComputedStyle(el);
                        if (style.cursor !== 'pointer') return;
                        
                        const rect = el.getBoundingClientRect();
                        
                        // 在 balanced 模式下，pointer 元素需满足附加条件
                        if (markMode === 'balanced') {{
                            // 必须有文本或 aria-label
                            const text = (el.innerText || el.textContent || '').trim();
                            const hasValidText = text.length > 0 && text.length < 200;
                            const hasAriaLabel = el.hasAttribute('aria-label');
                            
                            // 尺寸合理（不能太大，避免容器）
                            const isReasonableSize = rect.width < 600 && rect.height < 300;
                            
                            // 不是纯容器（子元素不能太多）
                            const childCount = el.children.length;
                            const notPureContainer = childCount < 10;
                            
                            if ((hasValidText || hasAriaLabel) && isReasonableSize && notPureContainer) {{
                                candidateSet.add(el);
                            }}
                        }} else {{
                            // all 模式：直接添加
                            candidateSet.add(el);
                        }}
                    }} catch(e) {{}}
                }});
            }}
            
            const candidates = Array.from(candidateSet);
            
            // ========== 2. 过滤和打分 ==========
            const isInputable = (el) => {{
                const tag = el.tagName.toLowerCase();
                if (tag === 'input') {{
                    const type = (el.type || 'text').toLowerCase();
                    return !['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'file', 'hidden'].includes(type);
                }}
                if (tag === 'textarea' || tag === 'select') return true;
                if (el.getAttribute('contenteditable') === 'true') return true;
                if (el.getAttribute('role') === 'textbox') return true;
                return false;
            }};
            
            const isCanvasOrSvg = (el) => {{
                const tag = el.tagName.toLowerCase();
                return tag === 'canvas' || tag === 'svg';
            }};
            
            const scored = [];
            
            candidates.forEach(el => {{
                try {{
                    const rect = el.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    
                    // 过滤不可见或太小的元素
                    if (area < minArea) return;
                    if (rect.bottom < 0 || rect.top > window.innerHeight) return;
                    if (rect.right < 0 || rect.left > window.innerWidth) return;
                    
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return;
                    if (parseFloat(style.opacity) === 0) return;
                    
                    // 计算分数
                    let score = 0;
                    const tag = el.tagName.toLowerCase();
                    
                    // 标签类型权重（核心交互元素得分最高）
                    const tagScores = {{
                        'button': 100,
                        'a': 95,
                        'input': 90,
                        'textarea': 88,
                        'select': 85,
                        'canvas': 75,
                        'video': 72,
                        'audio': 70,
                        'svg': 65,
                        'img': 50
                    }};
                    score += tagScores[tag] || 30;
                    
                    // ARIA role 加分
                    const role = el.getAttribute('role');
                    const roleScores = {{
                        'button': 25,
                        'link': 22,
                        'textbox': 20,
                        'checkbox': 18,
                        'radio': 18,
                        'switch': 18,
                        'menuitem': 15,
                        'tab': 15,
                        'option': 12,
                        'slider': 12,
                        'combobox': 12
                    }};
                    if (role && roleScores[role]) {{
                        score += roleScores[role];
                    }}
                    
                    // 有有效文本内容加分
                    const text = (el.innerText || el.textContent || '').trim();
                    if (text.length > 0 && text.length < 100) {{
                        score += 15;
                    }}
                    
                    // aria-label 加分
                    if (el.hasAttribute('aria-label')) {{
                        score += 12;
                    }}
                    
                    // tabindex 加分（可键盘访问）
                    const tabindex = el.getAttribute('tabindex');
                    if (tabindex !== null && parseInt(tabindex) >= 0) {{
                        score += 8;
                    }}
                    
                    // onclick 加分
                    if (el.hasAttribute('onclick')) {{
                        score += 10;
                    }}
                    
                    // 可输入元素额外加分
                    const inputable = isInputable(el);
                    if (inputable) {{
                        score += 20;
                    }}
                    
                    // 面积适中的元素加分（太大可能是容器）
                    if (area > 500 && area < 50000) {{
                        score += 5;
                    }}
                    
                    scored.push({{
                        el: el,
                        rect: {{
                            left: rect.left,
                            top: rect.top,
                            right: rect.right,
                            bottom: rect.bottom,
                            width: rect.width,
                            height: rect.height
                        }},
                        score: score,
                        inputable: inputable,
                        isCanvas: isCanvasOrSvg(el)
                    }});
                }} catch(e) {{}}
            }});
            
            // ========== 3. NMS 去重（按分数降序，抑制重叠的低分元素）==========
            scored.sort((a, b) => b.score - a.score);
            
            const computeIoU = (r1, r2) => {{
                const x1 = Math.max(r1.left, r2.left);
                const y1 = Math.max(r1.top, r2.top);
                const x2 = Math.min(r1.right, r2.right);
                const y2 = Math.min(r1.bottom, r2.bottom);
                
                if (x2 <= x1 || y2 <= y1) return 0;
                
                const intersection = (x2 - x1) * (y2 - y1);
                const area1 = r1.width * r1.height;
                const area2 = r2.width * r2.height;
                const union = area1 + area2 - intersection;
                
                return intersection / union;
            }};
            
            // 检查元素是否被另一个元素完全包含
            const isContainedBy = (inner, outer) => {{
                return inner.left >= outer.left &&
                       inner.right <= outer.right &&
                       inner.top >= outer.top &&
                       inner.bottom <= outer.bottom;
            }};
            
            const kept = [];
            scored.forEach(item => {{
                let shouldKeep = true;
                
                for (const k of kept) {{
                    const iou = computeIoU(item.rect, k.rect);
                    
                    // 如果 IoU 超过阈值，抑制低分元素
                    if (iou > iouThreshold) {{
                        shouldKeep = false;
                        break;
                    }}
                    
                    // 如果当前元素被保留的元素完全包含，也抑制（避免父子重复）
                    if (isContainedBy(item.rect, k.rect) || isContainedBy(k.rect, item.rect)) {{
                        // 较小的元素通常更具体，但分数高的优先
                        const itemArea = item.rect.width * item.rect.height;
                        const kArea = k.rect.width * k.rect.height;
                        
                        // 如果当前元素被包含且分数不够高，抑制
                        if (isContainedBy(item.rect, k.rect) && item.score < k.score + 20) {{
                            shouldKeep = false;
                            break;
                        }}
                    }}
                }}
                
                if (shouldKeep) {{
                    kept.push(item);
                }}
            }});
            
            // ========== 4. Top-N 截断 ==========
            const finalElements = kept.slice(0, maxMarks);
            
            // ========== 5. 渲染标签 ==========
            let currentId = startId;
            
            finalElements.forEach(item => {{
                const el = item.el;
                const rect = item.rect;
                
                // 设置元素属性
                el.setAttribute('data-ai-id', currentId);
                el.setAttribute('data-ai-inputable', item.inputable ? 'true' : 'false');
                el.setAttribute('data-ai-canvas', item.isCanvas ? 'true' : 'false');
                
                // 创建标签元素
                const tag = document.createElement('div');
                tag.className = 'ai-mark';
                
                // 标签文本格式：可输入用 [id]，Canvas/SVG 用 <id>，其他用纯数字
                if (item.inputable) {{
                    tag.textContent = '[' + currentId + ']';
                }} else if (item.isCanvas) {{
                    tag.textContent = '<' + currentId + '>';
                }} else {{
                    tag.textContent = currentId;
                }}
                
                // 样式：可输入元素用绿色，Canvas/SVG 用蓝色，其他用红色
                let bgColor;
                if (item.inputable) {{
                    bgColor = 'rgba(34, 139, 34, 0.9)';  // 绿色
                }} else if (item.isCanvas) {{
                    bgColor = 'rgba(30, 144, 255, 0.9)';  // 蓝色
                }} else {{
                    bgColor = 'rgba(220, 20, 60, 0.9)';  // 红色
                }}
                
                tag.style.cssText = `
                    position: fixed;
                    left: ${{rect.left}}px;
                    top: ${{rect.top}}px;
                    z-index: 2147483647;
                    background: ${{bgColor}};
                    color: white;
                    font-size: 12px;
                    padding: 1px 3px;
                    border-radius: 2px;
                    pointer-events: none;
                    border: 1px solid white;
                    font-family: sans-serif;
                    font-weight: bold;
                    line-height: 1.2;
                `;
                
                document.body.appendChild(tag);
                currentId++;
            }});
            
            return finalElements.length;
        }}
        """
    
    async def get_marked_screenshot(self) -> Tuple[Optional[bytes], str]:
        """获取带有元素标记的页面截图（支持跨 Frame）
        
        Returns:
            Tuple[Optional[bytes], str]: (截图数据, 状态信息)
            
        注意：
        - 截图使用 scale='css' 确保截图像素坐标与 CSS 坐标系一致
        - 这解决了 DPR（设备像素比）导致的坐标偏差问题
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            total_marked = 0
            current_id = 0
            
            # 递归遍历所有 Frame 进行标记
            # page.frames 包含 main frame 和所有 child frames
            for frame in self.page.frames:
                try:
                    if not frame.is_detached():
                        # 在当前 Frame 执行标记脚本
                        count = await frame.evaluate(self._get_mark_script(current_id))
                        total_marked += count
                        current_id += count
                except Exception as e:
                    # Frame 可能在遍历过程中销毁或不可访问（如跨域限制 strict 模式，但 Playwright 通常能处理）
                    logger.debug(f"Failed to mark frame {frame.name}: {e}")
            
            # 等待标记渲染完成
            await asyncio.sleep(0.1)
            
            # 截图 - 使用 scale='css' 确保坐标系一致
            try:
                screenshot = await self.page.screenshot(type='png', scale='css')
            except TypeError:
                # 兼容旧版 playwright 没有 scale 参数
                screenshot = await self.page.screenshot(type='png')
            
            # 获取当前分辨率和 DPR 信息
            viewport_info = ""
            if self.page.viewport_size:
                w = self.page.viewport_size['width']
                h = self.page.viewport_size['height']
                viewport_info = f"\n当前分辨率: {w}x{h}"
            
            # 获取 DPR 信息（便于排查坐标问题）
            try:
                dpr = await self.page.evaluate("window.devicePixelRatio")
                viewport_info += f"\nDPR: {dpr}"
            except Exception:
                pass
            
            return screenshot, f"已标记 {total_marked} 个可交互元素。{viewport_info}"
            
        except Exception as e:
            logger.error(f"Failed to get marked screenshot: {e}")
            return None, f"截图失败: {e}"
    
    async def navigate(self, url: str) -> Tuple[Optional[bytes], str]:
        """导航到指定 URL
        
        包含 SSRF 防护：
        - 验证 URL scheme 只允许 http/https
        - 拒绝访问私有网络地址（除非明确允许）
        - DNS 解析后验证 IP 地址
        - 支持域名白名单/黑名单
        """
        if not await self._init_browser():
            return None, "浏览器初始化失败。请确保已安装 Playwright 并运行 `playwright install chromium`。"
        
        try:
            # 确保 URL 有协议前缀
            if not url.startswith(('http://', 'https://', 'file://')):
                url = 'https://' + url
            
            # === SSRF 防护：验证 URL 安全性 ===
            # 初始化验证器（如果尚未配置）
            if self._url_validator is None:
                self._url_validator = URLValidator(
                    allow_private_network=self.allow_private_network,
                    allowed_domains=self.allowed_domains,
                    blocked_domains=self.blocked_domains
                )
            
            # 执行 URL 安全验证
            is_safe, validation_message = await self._url_validator.validate_url(url)
            if not is_safe:
                logger.warning(f"Browser SSRF protection blocked URL: {url} - {validation_message}")
                return None, f"🛡️ 安全限制：{validation_message}"
            
            logger.debug(f"Browser URL validation passed: {url}")
            # === SSRF 防护结束 ===
            
            await self.page.goto(url, wait_until='domcontentloaded', timeout=30000)
            
            # 等待页面稳定
            await asyncio.sleep(1)
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            title = await self.page.title()
            return screenshot, f"已打开: {title}\n{info}"
            
        except Exception as e:
            logger.error(f"Failed to navigate to {url}: {e}")
            return None, f"导航失败: {e}"
    
    async def click_element(self, element_id: int) -> Tuple[Optional[bytes], str]:
        """点击指定 ID 的元素 (跨 Frame 查找)"""
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            target_element = None
            target_frame = None
            
            # 遍历所有 Frames 查找元素
            for frame in self.page.frames:
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
            
            # 点击元素
            await target_element.click()
            
            # 等待页面响应
            await asyncio.sleep(1)
            
            # 尝试等待网络空闲
            try:
                await self.page.wait_for_load_state('networkidle', timeout=5000)
            except:
                pass
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已点击元素 {element_id}。{info}"
            
        except Exception as e:
            logger.error(f"Failed to click element {element_id}: {e}")
            return None, f"点击失败: {e}"

    async def click_coordinates(self, x: int, y: int) -> Tuple[Optional[bytes], str]:
        """点击指定坐标 (x, y)
        
        Args:
            x: 横坐标
            y: 纵坐标
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            # 边界检查
            viewport = self.page.viewport_size
            if viewport:
                width = viewport['width']
                height = viewport['height']
                if not (0 <= x <= width and 0 <= y <= height):
                    logger.warning(f"Click coordinates ({x}, {y}) out of viewport ({width}x{height})")
                    # 继续尝试点击，也许用户意图是边缘
            
            # 移动鼠标并点击
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.1) # 短暂亦顿，模拟人类行为
            await self.page.mouse.click(x, y)
            
            # 等待页面响应
            await asyncio.sleep(1)
            
            # 尝试等待网络空闲
            try:
                await self.page.wait_for_load_state('networkidle', timeout=5000)
            except:
                pass
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已点击坐标 ({x}, {y})。{info}"
            
        except Exception as e:
            logger.error(f"Failed to click coordinates ({x}, {y}): {e}")
            return None, f"坐标点击失败: {e}"

    async def type_text(self, text: str) -> Tuple[Optional[bytes], str]:
        """直接在当前焦点处输入文本"""
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            # 直接输入文本
            await self.page.keyboard.type(text, delay=20)
            
            # 等待一下
            await asyncio.sleep(0.5)
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已在当前焦点处输入文本。{info}"
            
        except Exception as e:
            logger.error(f"Failed to type text: {e}")
            return None, f"输入失败: {e}"

    async def input_text(self, element_id: int, text: str) -> Tuple[Optional[bytes], str]:
        """在指定元素中输入文本 (跨 Frame 查找)
        
        支持多种输入方式：
        1. 标准 fill() - 适用于 input/textarea/select/[contenteditable]
        2. click + type - 适用于自定义输入组件
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            target_element = None
            target_frame = None
            
            # 遍历所有 Frames 查找元素
            for frame in self.page.frames:
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
            
            # 检查元素是否被标记为可输入
            is_inputable = await target_element.get_attribute('data-ai-inputable')
            
            # 获取元素信息用于错误提示
            element_info = await target_frame.evaluate(f"""
                () => {{
                    const el = document.querySelector('[data-ai-id="{element_id}"]');
                    if (!el) return null;
                    return {{
                        tagName: el.tagName.toLowerCase(),
                        type: el.type || '',
                        role: el.getAttribute('role') || '',
                        contentEditable: el.getAttribute('contenteditable')
                    }};
                }}
            """)
            
            input_success = False
            method_used = ""
            
            # 方法1: 尝试标准 fill() - 适用于 input/textarea/select/[contenteditable]
            if is_inputable == 'true':
                try:
                    await target_element.fill(text)
                    input_success = True
                    method_used = "fill"
                except Exception as fill_error:
                    logger.debug(f"fill() failed for element {element_id}: {fill_error}")
            
            # 方法2: 如果 fill() 失败或元素不是标准可输入类型，尝试 click + type
            if not input_success:
                try:
                    # 先点击元素获取焦点
                    await target_element.click()
                    await asyncio.sleep(0.2)
                    
                    # 全选现有内容（如果有）
                    await self.page.keyboard.press('Control+A')
                    await asyncio.sleep(0.1)
                    
                    # 输入新文本（会替换选中内容）
                    await self.page.keyboard.type(text, delay=20)
                    input_success = True
                    method_used = "click+type"
                except Exception as type_error:
                    logger.debug(f"click+type failed for element {element_id}: {type_error}")
            
            # 方法3: 如果以上都失败，尝试直接设置 value 属性（仅适用于部分元素）
            if not input_success:
                try:
                    await target_frame.evaluate(f"""
                        () => {{
                            const el = document.querySelector('[data-ai-id="{element_id}"]');
                            if (el) {{
                                // 尝试设置 value
                                if ('value' in el) {{
                                    el.value = {repr(text)};
                                    // 触发 input 和 change 事件
                                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    return true;
                                }}
                                // 尝试设置 innerText（用于 contenteditable）
                                if (el.getAttribute('contenteditable') === 'true') {{
                                    el.innerText = {repr(text)};
                                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    return true;
                                }}
                            }}
                            return false;
                        }}
                    """)
                    input_success = True
                    method_used = "js-value"
                except Exception as js_error:
                    logger.debug(f"JS value setting failed for element {element_id}: {js_error}")
            
            if not input_success:
                # 提供详细的错误信息
                tag_info = f"{element_info['tagName']}" if element_info else "unknown"
                if element_info and element_info.get('type'):
                    tag_info += f"[type={element_info['type']}]"
                
                error_msg = (
                    f"无法在元素 {element_id} ({tag_info}) 中输入文本。"
                    f"该元素可能不是可输入的元素。"
                )
                
                if is_inputable != 'true':
                    error_msg += f"\n\n提示：此元素未被标记为可输入元素（绿色 [ID] 标记）。"
                    error_msg += f"请检查是否选择了正确的元素。可输入元素在截图中显示为绿色 [数字] 标记。"
                
                return None, error_msg
            
            # 等待一下
            await asyncio.sleep(0.5)
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已在元素 {element_id} 中输入文本（使用 {method_used} 方式）。{info}"
            
        except Exception as e:
            logger.error(f"Failed to input text to element {element_id}: {e}")
            return None, f"输入失败: {e}"
    
    async def scroll(self, direction: str) -> Tuple[Optional[bytes], str]:
        """滚动页面"""
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            direction = direction.lower().strip()
            
            if direction == 'up':
                await self.page.evaluate('window.scrollBy(0, -window.innerHeight)')
            elif direction == 'down':
                await self.page.evaluate('window.scrollBy(0, window.innerHeight)')
            elif direction == 'top':
                await self.page.evaluate('window.scrollTo(0, 0)')
            elif direction == 'bottom':
                await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            else:
                return None, f"不支持的滚动方向: {direction}。支持: up, down, top, bottom"
            
            # 等待滚动完成
            await asyncio.sleep(0.5)
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已向 {direction} 滚动。{info}"
            
        except Exception as e:
            logger.error(f"Failed to scroll {direction}: {e}")
            return None, f"滚动失败: {e}"
    
    async def get_element_info(self, element_id: int) -> Tuple[Optional[Dict[str, Any]], str]:
        """获取指定元素的信息 (跨 Frame 查找)"""
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            target_frame = None
            
            # 遍历寻找 Frame
            for frame in self.page.frames:
                try:
                    if frame.is_detached():
                        continue
                    if await frame.query_selector(f'[data-ai-id="{element_id}"]'):
                        target_frame = frame
                        break
                except:
                    continue
            
            if not target_frame:
                return None, f"未找到 ID 为 {element_id} 的元素。"
                
            info = await target_frame.evaluate(f"""
                () => {{
                    const el = document.querySelector('[data-ai-id="{element_id}"]');
                    if (!el) return null;
                    
                    return {{
                        tagName: el.tagName.toLowerCase(),
                        text: el.innerText || el.textContent || '',
                        href: el.href || el.getAttribute('href') || '',
                        src: el.src || el.getAttribute('src') || '',
                        alt: el.alt || el.getAttribute('alt') || '',
                        title: el.title || el.getAttribute('title') || '',
                        placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                        value: el.value || '',
                        type: el.type || el.getAttribute('type') || ''
                    }};
                }}
            """)
            
            if info is None:
                return None, f"未找到 ID 为 {element_id} 的元素。"
            
            # 构建描述
            desc_parts = [f"标签: {info['tagName']}"]
            if info['text']:
                text = info['text'][:100] + ('...' if len(info['text']) > 100 else '')
                desc_parts.append(f"文本: {text}")
            if info['href']:
                desc_parts.append(f"链接: {info['href']}")
            if info['src']:
                desc_parts.append(f"资源: {info['src']}")
            if info['alt']:
                desc_parts.append(f"描述: {info['alt']}")
            if info['placeholder']:
                desc_parts.append(f"占位符: {info['placeholder']}")
            
            return info, "\n".join(desc_parts)
            
        except Exception as e:
            logger.error(f"Failed to get element info for {element_id}: {e}")
            return None, f"获取元素信息失败: {e}"
    
    async def screenshot_element(self, element_id: int) -> Tuple[Optional[bytes], str]:
        """获取指定元素的干净截图（不含标记，跨 Frame）
        
        注意：使用 scale='css' 确保截图坐标系与 CSS 坐标系一致
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            target_element = None
            target_frame = None
            
            # 遍历寻找 Frame 和 Element
            for frame in self.page.frames:
                try:
                    if frame.is_detached():
                        continue
                    element = await frame.query_selector(f'[data-ai-id="{element_id}"]')
                    if element:
                        target_element = element
                        target_frame = frame
                        break
                except:
                    continue
            
            if not target_element:
                return None, f"未找到 ID 为 {element_id} 的元素。"
            
            # 1. 隐藏所有 Frame 的标记
            for frame in self.page.frames:
                try:
                    if not frame.is_detached():
                        await frame.evaluate("""
                            () => {
                                document.querySelectorAll('.ai-mark').forEach(e => e.style.display = 'none');
                            }
                        """)
                except:
                    pass
            
            # 2. 截图元素 - 使用 scale='css' 确保坐标系一致
            try:
                screenshot = await target_element.screenshot(type='png', scale='css')
            except TypeError:
                # 兼容旧版 playwright 没有 scale 参数
                screenshot = await target_element.screenshot(type='png')
            
            # 3. 恢复所有 Frame 的标记显示
            for frame in self.page.frames:
                try:
                    if not frame.is_detached():
                        await frame.evaluate("""
                            () => {
                                document.querySelectorAll('.ai-mark').forEach(e => e.style.display = '');
                            }
                        """)
                except:
                    pass
            
            return screenshot, f"已获取元素 {element_id} 的图片。"
            
        except Exception as e:
            logger.error(f"Failed to screenshot element {element_id}: {e}")
            # 尝试恢复标记
            try:
                for frame in self.page.frames:
                    if not frame.is_detached():
                        await frame.evaluate("() => { document.querySelectorAll('.ai-mark').forEach(e => e.style.display = ''); }")
            except:
                pass
            return None, f"元素截图失败: {e}"
    
    async def click_in_element(self, element_id: int, rx: float, ry: float) -> Tuple[Optional[bytes], str]:
        """在指定元素内的相对位置点击（跨 Frame 查找）
        
        适用于 Canvas、地图、图表等无法标记内部元素的场景。
        使用相对坐标 (0~1) 而非绝对像素坐标，更容易定位。
        
        Args:
            element_id: 元素 ID (data-ai-id)
            rx: 相对 X 坐标 (0.0 ~ 1.0)，0 表示元素最左侧，1 表示最右侧
            ry: 相对 Y 坐标 (0.0 ~ 1.0)，0 表示元素最顶部，1 表示最底部
            
        Returns:
            Tuple[Optional[bytes], str]: (截图数据, 状态信息)
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            target_element = None
            target_frame = None
            
            # 遍历所有 Frames 查找元素
            for frame in self.page.frames:
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
            
            # 获取元素的边界框
            bounding_box = await target_element.bounding_box()
            if not bounding_box:
                return None, f"无法获取元素 {element_id} 的边界框。"
            
            # 边界检查
            rx = max(0.0, min(1.0, rx))
            ry = max(0.0, min(1.0, ry))
            
            # 计算绝对坐标
            # bounding_box: {'x': left, 'y': top, 'width': w, 'height': h}
            abs_x = bounding_box['x'] + bounding_box['width'] * rx
            abs_y = bounding_box['y'] + bounding_box['height'] * ry
            
            # 移动鼠标并点击
            await self.page.mouse.move(abs_x, abs_y)
            await asyncio.sleep(0.1)  # 短暂停顿，模拟人类行为
            await self.page.mouse.click(abs_x, abs_y)
            
            # 等待页面响应
            await asyncio.sleep(1)
            
            # 尝试等待网络空闲
            try:
                await self.page.wait_for_load_state('networkidle', timeout=5000)
            except:
                pass
            
            # 获取标记截图
            screenshot, info = await self.get_marked_screenshot()
            
            return screenshot, f"已在元素 {element_id} 内的相对位置 ({rx:.2f}, {ry:.2f}) 点击（绝对坐标: {abs_x:.0f}, {abs_y:.0f}）。{info}"
            
        except Exception as e:
            logger.error(f"Failed to click in element {element_id} at ({rx}, {ry}): {e}")
            return None, f"元素内点击失败: {e}"
    
    async def crop_screenshot(self, x: int, y: int, width: int, height: int, scale: float = 2.0) -> Tuple[Optional[bytes], str]:
        """裁剪并放大页面指定区域的截图
        
        适用于需要精确定位的场景，如小按钮、验证码等。
        先获取区域截图，然后可以更精确地识别目标位置。
        
        Args:
            x: 裁剪区域左上角 X 坐标
            y: 裁剪区域左上角 Y 坐标
            width: 裁剪区域宽度
            height: 裁剪区域高度
            scale: 放大倍数（默认 2.0）
            
        Returns:
            Tuple[Optional[bytes], str]: (截图数据, 状态信息)
        """
        if not self.page:
            return None, "浏览器未初始化。"
        
        try:
            # 参数校验
            viewport = self.page.viewport_size
            if viewport:
                max_width = viewport['width']
                max_height = viewport['height']
                
                # 确保裁剪区域在视口内
                x = max(0, min(x, max_width - 1))
                y = max(0, min(y, max_height - 1))
                width = min(width, max_width - x)
                height = min(height, max_height - y)
            
            # 限制放大倍数
            scale = max(1.0, min(4.0, scale))
            
            # 使用 clip 参数截取指定区域
            try:
                screenshot = await self.page.screenshot(
                    type='png',
                    scale='css',
                    clip={
                        'x': x,
                        'y': y,
                        'width': width,
                        'height': height
                    }
                )
            except TypeError:
                # 兼容旧版 playwright
                screenshot = await self.page.screenshot(
                    type='png',
                    clip={
                        'x': x,
                        'y': y,
                        'width': width,
                        'height': height
                    }
                )
            
            # 如果需要放大，使用 PIL 处理
            if scale > 1.0:
                try:
                    from PIL import Image
                    import io
                    
                    img = Image.open(io.BytesIO(screenshot))
                    new_width = int(img.width * scale)
                    new_height = int(img.height * scale)
                    img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    
                    output = io.BytesIO()
                    img_resized.save(output, format='PNG')
                    screenshot = output.getvalue()
                except ImportError:
                    logger.warning("PIL not available, returning unscaled screenshot")
                    scale = 1.0
            
            return screenshot, f"已裁剪区域 ({x}, {y}) - ({x+width}, {y+height})，放大 {scale}x。\n提示：在此区域内，坐标 (0,0) 对应原图的 ({x}, {y})。"
            
        except Exception as e:
            logger.error(f"Failed to crop screenshot: {e}")
            return None, f"裁剪截图失败: {e}"
    
    async def get_page_info(self) -> Dict[str, Any]:
        """获取当前页面信息"""
        if not self.page:
            return {"error": "浏览器未初始化"}
        
        try:
            return {
                "url": self.page.url,
                "title": await self.page.title(),
                "viewport": {"width": self.viewport_width, "height": self.viewport_height}
            }
        except Exception as e:
            return {"error": str(e)}
    
    @property
    def is_active(self) -> bool:
        """检查浏览器是否处于活动状态"""
        return self.page is not None and self.current_user is not None


# 创建全局单例
browser_manager = BrowserManager()