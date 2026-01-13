import re
import fnmatch
from typing import List, Any, Tuple, Optional
from astrbot.api import logger
from astrbot.api import message_components as Comp


def _unwrap_onebot_response(resp: Any) -> Any:
    """兼容不同 OneBot 实现的返回格式。

    有些实现会返回:
        {"status":"ok","retcode":0,"data":{...}}
    有些实现会直接返回 data 本体。

    这里仅在检测到典型包装字段时才解包，避免误伤业务数据里真实存在的 'data' 字段。
    """
    if isinstance(resp, dict) and "data" in resp:
        if any(k in resp for k in ("retcode", "status", "msg", "wording")):
            return resp.get("data")
    return resp


async def call_onebot(client, action: str, **kwargs) -> Any:
    """OneBot API 兼容层调用函数

    封装 OneBot API 调用，自动处理不同 AstrBot 版本/OneBot 实现的兼容性问题。
    优先使用 client.call_action，如果失败则 fallback 到 client.api.call_action。

    Args:
        client: OneBot 客户端实例 (event.bot)
        action: API 动作名称，如 'get_msg', 'delete_msg' 等
        **kwargs: API 参数

    Returns:
        API 调用返回结果（已尽量解包到 data 本体）

    Raises:
        Exception: 当所有调用方式都失败时抛出最后的异常
    """
    # 优先尝试 client.call_action (较新的 AstrBot 版本)
    if hasattr(client, 'call_action') and callable(getattr(client, 'call_action', None)):
        try:
            resp = await client.call_action(action, **kwargs)
            return _unwrap_onebot_response(resp)
        except AttributeError:
            # call_action 存在但调用失败，尝试 fallback
            pass
        except Exception as e:
            # 其他异常（如 API 本身的错误），先尝试 fallback
            # 如果 fallback 也失败，则抛出原始异常
            if hasattr(client, 'api') and hasattr(client.api, 'call_action'):
                try:
                    resp = await client.api.call_action(action, **kwargs)
                    return _unwrap_onebot_response(resp)
                except Exception:
                    # fallback 也失败，抛出原始异常
                    raise e
            raise

    # Fallback 到 client.api.call_action (兼容旧版本)
    if hasattr(client, 'api') and hasattr(client.api, 'call_action'):
        resp = await client.api.call_action(action, **kwargs)
        return _unwrap_onebot_response(resp)

    # 两种方式都不可用
    raise AttributeError(
        f"OneBot client 不支持 call_action 或 api.call_action 方法。"
        f"Client type: {type(client).__name__}"
    )


async def delete_single_message(client, message_id: str) -> str:
    """内部方法：撤回单条消息"""
    # 尝试先转为 int
    try:
        await call_onebot(client, 'delete_msg', message_id=int(message_id))
        return "已撤回 (int)"
    except Exception as e_int:
        logger.debug(f"Failed to delete message {message_id} as int: {e_int}")
        
        # 尝试直接传 string
        try:
            await call_onebot(client, 'delete_msg', message_id=str(message_id))
            return "已撤回 (str)"
        except Exception as e_str:
            logger.debug(f"Failed to delete message {message_id} as str: {e_str}")
            
            # 如果包含下划线（例如 12345_6789），尝试只取第一部分
            if "_" in str(message_id):
                try:
                    real_id = int(str(message_id).split("_")[0])
                    logger.info(f"Trying to delete message with split ID: {real_id}")
                    await call_onebot(client, 'delete_msg', message_id=real_id)
                    return f"已撤回 (split int: {real_id})"
                except Exception as e_split:
                    logger.error(f"Failed to delete message {real_id} as split int: {e_split}")
                    raise e_split
            else:
                # 如果所有尝试都失败，记录最后的错误
                logger.error(f"Failed to delete message {message_id}: {e_str}")
                raise e_str

def parse_at_content(text: str) -> List[Comp.BaseMessageComponent]:
    """解析文本中的 [At:123456]"""
    chain = []
    pattern = r"\[At:(\d+)\]"
    
    last_end = 0
    matches = list(re.finditer(pattern, text))
    
    if not matches:
        chain.append(Comp.Plain(text))
        return chain

    for match in matches:
        start, end = match.span()
        qq = match.group(1)
        
        # 添加匹配前的文本
        if start > last_end:
            chain.append(Comp.Plain(text[last_end:start]))
        
        # 添加 At 组件
        chain.append(Comp.At(qq=qq))
        # 使用 \u200b (零宽空格) 包裹空格，防止被 adapter strip 掉
        chain.append(Comp.Plain("\u200b \u200b"))
        
        last_end = end
    
    # 添加剩余的文本
    if last_end < len(text):
        chain.append(Comp.Plain(text[last_end:]))
        
    return chain

def get_qq_string_length(text: str) -> int:
    """计算 QQ 字符串长度 (UTF-8 字节数)"""
    try:
        return len(text.encode('utf-8'))
    except:
        return len(text)

def truncate_qq_string(text: str, max_length: int = 60) -> str:
    """截断字符串以符合 QQ 长度限制"""
    encoded = text.encode('utf-8')
    if len(encoded) <= max_length:
        return text
    
    # 截取前 max_length 个字节，并忽略错误的解码（防止截断多字节字符）
    # decode('utf-8', 'ignore') 会丢弃末尾不完整的字节
    return encoded[:max_length].decode('utf-8', 'ignore')

def parse_leaked_tool_call(text: str, filter_patterns: List[str] = None) -> tuple[str | None, str | None]:
    """
    尝试解析泄露到文本中的工具调用。
    例如: default_api:reply_message{content: <ctrl46>...<ctrl46>, message_id: ...}
    返回: (content, message_id) or (None, None)
    
    解析策略：
    1. 优先尝试 JSON 解析（更稳健）
    2. 正则仅作为 fallback
    """
    if "default_api:reply_message" not in text:
        return None, None
    
    # 清理文本
    clean_text = text.replace("\n", "").replace("\r", "")
    # 将 <ctrl46> 替换为引号，方便解析
    clean_text = clean_text.replace("<ctrl46>", '"')
    
    content = None
    message_id = None
    
    # 策略1: 尝试 JSON 解析（优先）
    json_result = _try_parse_as_json(clean_text)
    if json_result:
        content, message_id = json_result
    
    # 策略2: 正则提取作为 fallback
    if content is None and message_id is None:
        content, message_id = _parse_with_regex(clean_text)
    
    if not content and not message_id:
        return None, None
    
    # 清理内容
    if content:
        if filter_patterns:
            for pattern in filter_patterns:
                try:
                    content = re.sub(pattern, "", content)
                except Exception as e:
                    logger.error(f"Invalid regex pattern {pattern}: {e}")
        elif filter_patterns is None:
            # 兼容旧行为，默认过滤 &&tag&&
            content = re.sub(r"&&.*?&&", "", content)
            
        content = content.strip()
        
    return content, message_id


def _try_parse_as_json(text: str) -> Optional[Tuple[str, str]]:
    """尝试从文本中提取并解析 JSON 对象
    
    Args:
        text: 清理后的文本
        
    Returns:
        (content, message_id) 或 None
    """
    import json
    
    # 尝试找到 JSON 对象的边界 { ... }
    # 支持多种格式：
    # - default_api:reply_message{...}
    # - default_api:reply_message {...}
    # - 直接 {...}
    
    json_patterns = [
        r'default_api:reply_message\s*(\{.*\})',  # 紧跟或有空格
        r'reply_message\s*(\{.*\})',
        r'(\{[^{}]*"content"[^{}]*"message_id"[^{}]*\})',  # 包含两个关键字段的对象
        r'(\{[^{}]*"message_id"[^{}]*"content"[^{}]*\})',  # 顺序相反
    ]
    
    for pattern in json_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            json_str = match.group(1)
            try:
                # 尝试修复常见的非标准 JSON 问题
                # 1. 单引号替换为双引号
                fixed_json = json_str.replace("'", '"')
                # 2. 处理无引号的键名 (key: value -> "key": value)
                fixed_json = re.sub(r'(\w+)\s*:', r'"\1":', fixed_json)
                # 3. 移除可能的尾随逗号
                fixed_json = re.sub(r',\s*}', '}', fixed_json)
                fixed_json = re.sub(r',\s*]', ']', fixed_json)
                
                obj = json.loads(fixed_json)
                
                if isinstance(obj, dict):
                    content = obj.get('content')
                    msg_id = obj.get('message_id')
                    
                    # 确保提取到的值是字符串
                    if content is not None:
                        content = str(content)
                    if msg_id is not None:
                        msg_id = str(msg_id)
                    
                    if content or msg_id:
                        return content, msg_id
                        
            except (json.JSONDecodeError, ValueError, TypeError):
                # JSON 解析失败，继续尝试下一个模式
                continue
    
    return None


def _parse_with_regex(text: str) -> Tuple[Optional[str], Optional[str]]:
    """使用正则表达式提取 content 和 message_id
    
    Args:
        text: 清理后的文本
        
    Returns:
        (content, message_id)
    """
    content = None
    message_id = None
    
    # 严格匹配引号：只使用 " 或 '，不再包含 .
    # 使用非贪婪匹配，支持引号内的内容
    content_match = re.search(r'content\s*:\s*(["\'])(.*?)\1', text)
    id_match = re.search(r'message_id\s*:\s*(["\'])(.*?)\1', text)
    
    if content_match:
        content = content_match.group(2)
    if id_match:
        message_id = id_match.group(2)
    
    # Fallback: 无引号情况（纯数字 message_id）
    if not message_id:
        id_match_nq = re.search(r'message_id\s*:\s*(\d+)', text)
        if id_match_nq:
            message_id = id_match_nq.group(1)
    
    # Fallback: 无引号的 content（到逗号或右括号为止）
    if not content:
        content_match_nq = re.search(r'content\s*:\s*([^,}]+)', text)
        if content_match_nq:
            content = content_match_nq.group(1).strip().strip('"\'')
    
    return content, message_id


async def check_tool_permission(
    tool_name: str,
    event,  # AstrMessageEvent
    permission_config: dict,
    client = None  # OneBot client, 用于查询群角色
) -> Tuple[bool, Optional[str]]:
    """检查用户是否有权限使用指定工具
    
    Args:
        tool_name: 工具名称（不含前缀）
        event: 消息事件 (AstrMessageEvent)
        permission_config: 权限配置字典，来自 config.get("tool_permission", {})
        client: OneBot 客户端（用于查询群角色，可选）
        
    Returns:
        Tuple[bool, Optional[str]]: (是否有权限, 拒绝原因或None)
    """
    # 1. 如果启用了"无视权限检查"，直接放行
    if permission_config.get("llm_ignore_permission_check", False):
        return True, None
    
    # 2. 检查工具是否在受限列表中
    admin_only_tools = permission_config.get("admin_only_tools", [
        "ban_user", "group_ban", "delete_message",
        "change_group_card", "send_group_notice",
        "set_essence_message", "browser_*"
    ])
    
    is_restricted = False
    for pattern in admin_only_tools:
        if fnmatch.fnmatch(tool_name, pattern):
            is_restricted = True
            break
    
    # 如果工具不在受限列表中，直接放行
    if not is_restricted:
        return True, None
    
    sender_id = event.get_sender_id()
    
    # 3. 检查是否是 AstrBot 管理员
    if event.is_admin():
        return True, None
    
    # 4. 检查是否在用户白名单中
    allow_users = permission_config.get("tool_allow_users", [])
    if sender_id in allow_users or str(sender_id) in allow_users:
        return True, None
    
    # 5. 检查是否允许群管理员/群主，且发送者是群管理员/群主
    if permission_config.get("allow_group_admin", False):
        # 需要导入检查类型
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent) and event.get_group_id():
                if client:
                    try:
                        group_id = event.get_group_id()
                        member_info = await call_onebot(
                            client,
                            'get_group_member_info',
                            group_id=int(group_id),
                            user_id=int(sender_id),
                            no_cache=True
                        )
                        role = member_info.get('role', 'member')
                        if role in ['owner', 'admin']:
                            return True, None
                    except Exception as e:
                        logger.warning(f"Failed to check group role for permission: {e}")
        except ImportError:
            pass
    
    # 6. 权限检查失败
    return False, f"权限不足：工具 {tool_name} 仅限管理员或白名单用户使用。你的用户ID: {sender_id}，如需使用请联系管理员。"


def get_original_tool_name(tool_name: str, has_prefix: bool) -> str:
    """获取工具的原始名称（去除前缀）
    
    Args:
        tool_name: 当前工具名称（可能带有 qts_ 前缀）
        has_prefix: 是否启用了工具前缀
        
    Returns:
        str: 原始工具名称
    """
    if has_prefix and tool_name.startswith("qts_"):
        return tool_name[4:]
    return tool_name