/**
 * 元素标记脚本 - Visual Grounding for AI Browser Control
 * 
 * 此脚本用于在网页中标记可交互元素，使 AI 能够识别和操作页面元素。
 * 
 * 优化特性：
 * - 语义优先：优先收集强交互元素（button, a, input 等）
 * - 打分机制：根据元素类型、语义信息给予不同分数
 * - NMS去重：基于 IoU 重叠抑制，避免父子元素重复标记
 * - Top-N截断：限制最大标记数量，避免"满屏数字"
 * 
 * 模板变量（由 Python 替换）：
 * - {{START_ID}}: 起始标记 ID
 * - {{MAX_MARKS}}: 最大标记数量
 * - {{MIN_AREA}}: 最小元素面积（像素²）
 * - {{IOU_THRESHOLD}}: NMS IoU 阈值
 * - {{MARK_MODE}}: 标记模式 ("minimal" | "balanced" | "all")
 */
() => {
    const startId = {{START_ID}};
    const maxMarks = {{MAX_MARKS}};
    const minArea = {{MIN_AREA}};
    const iouThreshold = {{IOU_THRESHOLD}};
    const markMode = '{{MARK_MODE}}';
    
    // ========== 清理旧标记 ==========
    // 仅清理当前 Frame 的标记
    document.querySelectorAll('.ai-mark').forEach(e => e.remove());
    
    // 清理旧的元素标识，避免重复 data-ai-id 导致点击错位/点错
    document.querySelectorAll('[data-ai-id]').forEach(el => {
        el.removeAttribute('data-ai-id');
        el.removeAttribute('data-ai-inputable');
        el.removeAttribute('data-ai-canvas');
    });

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
    strongSelectors.forEach(sel => {
        try {
            document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
        } catch(e) {}
    });
    
    // 收集事件驱动元素
    eventSelectors.forEach(sel => {
        try {
            document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
        } catch(e) {}
    });
    
    // 收集特殊元素
    specialSelectors.forEach(sel => {
        try {
            document.querySelectorAll(sel).forEach(el => candidateSet.add(el));
        } catch(e) {}
    });
    
    // 在 balanced 和 all 模式下，收集 pointer 元素（需满足附加条件）
    if (markMode !== 'minimal') {
        const allElements = document.querySelectorAll('*');
        allElements.forEach(el => {
            if (candidateSet.has(el)) return;
            
            try {
                const style = window.getComputedStyle(el);
                if (style.cursor !== 'pointer') return;
                
                const rect = el.getBoundingClientRect();
                
                // 在 balanced 模式下，pointer 元素需满足附加条件
                if (markMode === 'balanced') {
                    // 必须有文本或 aria-label
                    const text = (el.innerText || el.textContent || '').trim();
                    const hasValidText = text.length > 0 && text.length < 200;
                    const hasAriaLabel = el.hasAttribute('aria-label');
                    
                    // 尺寸合理（不能太大，避免容器）
                    const isReasonableSize = rect.width < 600 && rect.height < 300;
                    
                    // 不是纯容器（子元素不能太多）
                    const childCount = el.children.length;
                    const notPureContainer = childCount < 10;
                    
                    if ((hasValidText || hasAriaLabel) && isReasonableSize && notPureContainer) {
                        candidateSet.add(el);
                    }
                } else {
                    // all 模式：直接添加
                    candidateSet.add(el);
                }
            } catch(e) {}
        });
    }
    
    const candidates = Array.from(candidateSet);
    
    // ========== 2. 过滤和打分 ==========
    const isInputable = (el) => {
        const tag = el.tagName.toLowerCase();
        if (tag === 'input') {
            const type = (el.type || 'text').toLowerCase();
            return !['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'file', 'hidden'].includes(type);
        }
        if (tag === 'textarea' || tag === 'select') return true;
        if (el.getAttribute('contenteditable') === 'true') return true;
        if (el.getAttribute('role') === 'textbox') return true;
        return false;
    };
    
    const isCanvasOrSvg = (el) => {
        const tag = el.tagName.toLowerCase();
        return tag === 'canvas' || tag === 'svg';
    };
    
    const scored = [];
    
    candidates.forEach(el => {
        try {
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
            const tagScores = {
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
            };
            score += tagScores[tag] || 30;
            
            // ARIA role 加分
            const role = el.getAttribute('role');
            const roleScores = {
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
            };
            if (role && roleScores[role]) {
                score += roleScores[role];
            }
            
            // 有有效文本内容加分
            const text = (el.innerText || el.textContent || '').trim();
            if (text.length > 0 && text.length < 100) {
                score += 15;
            }
            
            // aria-label 加分
            if (el.hasAttribute('aria-label')) {
                score += 12;
            }
            
            // tabindex 加分（可键盘访问）
            const tabindex = el.getAttribute('tabindex');
            if (tabindex !== null && parseInt(tabindex) >= 0) {
                score += 8;
            }
            
            // onclick 加分
            if (el.hasAttribute('onclick')) {
                score += 10;
            }
            
            // 可输入元素额外加分
            const inputable = isInputable(el);
            if (inputable) {
                score += 20;
            }
            
            // 面积适中的元素加分（太大可能是容器）
            if (area > 500 && area < 50000) {
                score += 5;
            }
            
            scored.push({
                el: el,
                rect: {
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                    width: rect.width,
                    height: rect.height
                },
                score: score,
                inputable: inputable,
                isCanvas: isCanvasOrSvg(el)
            });
        } catch(e) {}
    });
    
    // ========== 3. NMS 去重（按分数降序，抑制重叠的低分元素）==========
    scored.sort((a, b) => b.score - a.score);
    
    const computeIoU = (r1, r2) => {
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
    };
    
    // 检查元素是否被另一个元素完全包含
    const isContainedBy = (inner, outer) => {
        return inner.left >= outer.left &&
               inner.right <= outer.right &&
               inner.top >= outer.top &&
               inner.bottom <= outer.bottom;
    };
    
    const kept = [];
    scored.forEach(item => {
        let shouldKeep = true;
        
        for (const k of kept) {
            const iou = computeIoU(item.rect, k.rect);
            
            // 如果 IoU 超过阈值，抑制低分元素
            if (iou > iouThreshold) {
                shouldKeep = false;
                break;
            }
            
            // 如果当前元素被保留的元素完全包含，也抑制（避免父子重复）
            if (isContainedBy(item.rect, k.rect) || isContainedBy(k.rect, item.rect)) {
                // 较小的元素通常更具体，但分数高的优先
                const itemArea = item.rect.width * item.rect.height;
                const kArea = k.rect.width * k.rect.height;
                
                // 如果当前元素被包含且分数不够高，抑制
                if (isContainedBy(item.rect, k.rect) && item.score < k.score + 20) {
                    shouldKeep = false;
                    break;
                }
            }
        }
        
        if (shouldKeep) {
            kept.push(item);
        }
    });
    
    // ========== 4. Top-N 截断 ==========
    const finalElements = kept.slice(0, maxMarks);
    
    // ========== 5. 渲染标签 ==========
    let currentId = startId;
    
    finalElements.forEach(item => {
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
        if (item.inputable) {
            tag.textContent = '[' + currentId + ']';
        } else if (item.isCanvas) {
            tag.textContent = '<' + currentId + '>';
        } else {
            tag.textContent = currentId;
        }
        
        // 样式：可输入元素用绿色，Canvas/SVG 用蓝色，其他用红色
        let bgColor;
        if (item.inputable) {
            bgColor = 'rgba(34, 139, 34, 0.9)';  // 绿色
        } else if (item.isCanvas) {
            bgColor = 'rgba(30, 144, 255, 0.9)';  // 蓝色
        } else {
            bgColor = 'rgba(220, 20, 60, 0.9)';  // 红色
        }
        
        tag.style.cssText = `
            position: fixed;
            left: ${rect.left}px;
            top: ${rect.top}px;
            z-index: 2147483647;
            background: ${bgColor};
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
    });
    
    return finalElements.length;
}