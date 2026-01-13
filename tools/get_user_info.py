from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from ..utils import call_onebot

class GetUserInfoTool(FunctionTool):
    def __init__(self):
        super().__init__(
            name="get_user_info",
            description="查询QQ用户的详细资料（昵称、QQ号、等级、群名片、角色等）。支持批量查询。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标QQ号。如果不填，默认查询当前你自己的资料。可以填多个QQ号以批量查询，用逗号分隔。",
                    },
                },
                "required": [],
            }
        )
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        qq_id = kwargs.get("qq_id")
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持此操作 (仅支持 OneBot/Aiocqhttp)。"

        client = event.bot
        
        # 确定目标 QQ 列表
        target_ids = []
        if qq_id:
            # 支持中文逗号
            qq_id = qq_id.replace("，", ",")
            target_ids = [uid.strip() for uid in qq_id.split(",") if uid.strip()]
        else:
            target_ids = [event.get_sender_id()]

        if not target_ids:
            return "未指定查询对象。"

        results = []
        is_group = False
        group_id = None
        
        # 判断是否为群聊
        if hasattr(event.message_obj, "group_id") and event.message_obj.group_id:
            is_group = True
            group_id = event.message_obj.group_id

        # 如果是群聊，先获取龙王信息
        dragon_king_uin = None
        if is_group:
            try:
                honor_info = await call_onebot(client, 'get_group_honor_info', group_id=group_id, type="talkative")
                if honor_info and 'current_talkative' in honor_info:
                    dragon_king_uin = str(honor_info['current_talkative'].get('user_id', ''))
            except Exception as e:
                logger.warning(f"Failed to get group honor info: {e}")

        for uid in target_ids:
            info_str = ""
            try:
                if is_group:
                    # 群聊场景
                    member_info = await call_onebot(client, 'get_group_member_info', group_id=group_id, user_id=int(uid))
                    
                    nickname = member_info.get('nickname', '未知')
                    card = member_info.get('card', '')
                    role = member_info.get('role', 'member')
                    title = member_info.get('title', '')
                    level = member_info.get('level', '未知')
                    sex = member_info.get('sex', 'unknown')
                    age = member_info.get('age', '未知')
                    area = member_info.get('area', '')
                    
                    # 角色转换
                    role_map = {
                        "owner": "群主",
                        "admin": "管理员",
                        "member": "成员"
                    }
                    role_cn = role_map.get(role, "成员")
                    
                    is_dragon_king = "是" if str(uid) == str(dragon_king_uin) else "否"
                    
                    info_str = f"QQ: {uid}\n"
                    info_str += f"昵称: {nickname}\n"
                    if card:
                        info_str += f"群名片: {card}\n"
                    info_str += f"身份: {role_cn}\n"
                    if title:
                        info_str += f"头衔: {title}\n"
                    info_str += f"等级: {level}\n"
                    info_str += f"龙王: {is_dragon_king}\n"
                    
                    # 详细资料 (仅供参考)
                    details = []
                    if sex != 'unknown': details.append(f"性别: {sex}")
                    if age != '未知': details.append(f"年龄: {age}")
                    if area: details.append(f"地区: {area}")
                    
                    if details:
                        info_str += f"详细资料(仅供参考): {', '.join(details)}\n"

                else:
                    # 私聊场景
                    stranger_info = await call_onebot(client, 'get_stranger_info', user_id=int(uid))
                    nickname = stranger_info.get('nickname', '未知')
                    sex = stranger_info.get('sex', 'unknown')
                    age = stranger_info.get('age', '未知')
                    
                    info_str = f"QQ: {uid}\n"
                    info_str += f"昵称: {nickname}\n"
                    if sex != 'unknown':
                        info_str += f"性别: {sex}\n"
                    if age != '未知':
                        info_str += f"年龄: {age}\n"

                results.append(info_str)

            except Exception as e:
                logger.error(f"Failed to get info for {uid}: {e}")
                results.append(f"QQ {uid}: 获取资料失败 ({e})")

        # 返回纯文本结果
        return "\n---\n".join(results)