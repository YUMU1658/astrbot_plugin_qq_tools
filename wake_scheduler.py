"""
主动唤醒任务管理器

负责管理定时唤醒任务的创建、调度、持久化和触发。
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable, Awaitable, TYPE_CHECKING
from datetime import datetime

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.star import Context


@dataclass
class WakeTask:
    """唤醒任务数据结构"""
    task_id: str
    trigger_time: float  # Unix timestamp
    session_id: str  # unified_msg_origin
    platform_id: str  # 平台适配器 ID
    remark: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    triggered: bool = False  # 是否已触发
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "WakeTask":
        return cls(**data)
    
    def remaining_seconds(self) -> float:
        """返回距离触发的剩余秒数"""
        return max(0, self.trigger_time - time.time())
    
    def trigger_time_str(self) -> str:
        """返回可读的触发时间字符串"""
        dt = datetime.fromtimestamp(self.trigger_time)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    
    def format_display(self) -> str:
        """格式化显示信息"""
        remaining = int(self.remaining_seconds())
        if remaining > 0:
            if remaining >= 3600:
                remaining_str = f"{remaining // 3600}h{(remaining % 3600) // 60}m{remaining % 60}s"
            elif remaining >= 60:
                remaining_str = f"{remaining // 60}m{remaining % 60}s"
            else:
                remaining_str = f"{remaining}s"
        else:
            remaining_str = "已到期"
        
        remark_str = f" ({self.remark})" if self.remark else ""
        return f"[{self.task_id[:8]}] {self.trigger_time_str()} (剩余 {remaining_str}){remark_str}"


class WakeScheduler:
    """唤醒任务调度器"""
    
    def __init__(self, context: "Context", data_dir: str):
        self.context = context
        self.data_dir = data_dir
        self.data_file = os.path.join(data_dir, "wake_tasks.json")
        
        # 任务存储：task_id -> WakeTask
        self._tasks: Dict[str, WakeTask] = {}
        
        # 并发锁
        self._lock = asyncio.Lock()
        
        # 调度任务：task_id -> asyncio.Task
        self._scheduled_tasks: Dict[str, asyncio.Task] = {}
        
        # 唤醒回调函数（由插件设置）
        self._wake_callback: Optional[Callable[[WakeTask], Awaitable[None]]] = None
        
        # 是否已初始化
        self._initialized = False
    
    async def initialize(self):
        """初始化调度器，加载持久化数据并调度任务"""
        if self._initialized:
            return
        
        # 确保数据目录存在
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 加载持久化数据
        await self._load_tasks()
        
        # 调度所有未触发的任务
        await self._schedule_all_pending_tasks()
        
        self._initialized = True
        logger.info(f"WakeScheduler initialized with {len(self._tasks)} tasks")
    
    def set_wake_callback(self, callback: Callable[[WakeTask], Awaitable[None]]):
        """设置唤醒回调函数"""
        self._wake_callback = callback
    
    async def create_task(
        self,
        session_id: str,
        platform_id: str,
        delay_seconds: int,
        remark: Optional[str] = None
    ) -> str:
        """创建唤醒任务
        
        Args:
            session_id: 会话标识 (unified_msg_origin)
            platform_id: 平台适配器 ID
            delay_seconds: 延迟秒数
            remark: 备注
            
        Returns:
            task_id: 任务唯一标识
        """
        async with self._lock:
            task_id = str(uuid.uuid4())
            trigger_time = time.time() + delay_seconds
            
            task = WakeTask(
                task_id=task_id,
                trigger_time=trigger_time,
                session_id=session_id,
                platform_id=platform_id,
                remark=remark
            )
            
            self._tasks[task_id] = task
            await self._save_tasks()
            
            # 调度任务
            self._schedule_task(task)
            
            logger.info(f"Wake task created: {task_id} for session {session_id}, trigger at {task.trigger_time_str()}")
            return task_id
    
    async def delete_task(self, task_id: str, session_id: Optional[str] = None) -> bool:
        """删除唤醒任务
        
        Args:
            task_id: 任务 ID
            session_id: 如果指定，则只删除属于该会话的任务（用于 LLM 工具的会话隔离）
            
        Returns:
            是否成功删除
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            
            # 会话隔离检查
            if session_id and task.session_id != session_id:
                return False
            
            # 取消调度任务
            if task_id in self._scheduled_tasks:
                self._scheduled_tasks[task_id].cancel()
                del self._scheduled_tasks[task_id]
            
            del self._tasks[task_id]
            await self._save_tasks()
            
            logger.info(f"Wake task deleted: {task_id}")
            return True
    
    async def clear_tasks(self, session_id: Optional[str] = None) -> int:
        """清空唤醒任务
        
        Args:
            session_id: 如果指定，只清空该会话的任务；否则清空所有任务
            
        Returns:
            删除的任务数量
        """
        async with self._lock:
            if session_id:
                # 只清空指定会话的任务
                to_delete = [tid for tid, task in self._tasks.items() 
                           if task.session_id == session_id and not task.triggered]
            else:
                # 清空所有未触发的任务
                to_delete = [tid for tid, task in self._tasks.items() if not task.triggered]
            
            for task_id in to_delete:
                if task_id in self._scheduled_tasks:
                    self._scheduled_tasks[task_id].cancel()
                    del self._scheduled_tasks[task_id]
                del self._tasks[task_id]
            
            await self._save_tasks()
            
            logger.info(f"Cleared {len(to_delete)} wake tasks" + (f" for session {session_id}" if session_id else ""))
            return len(to_delete)
    
    def list_tasks(self, session_id: Optional[str] = None) -> List[WakeTask]:
        """列出唤醒任务
        
        Args:
            session_id: 如果指定，只列出该会话的任务
            
        Returns:
            未触发的任务列表，按触发时间排序
        """
        tasks = [task for task in self._tasks.values() if not task.triggered]
        
        if session_id:
            tasks = [task for task in tasks if task.session_id == session_id]
        
        # 按触发时间排序
        tasks.sort(key=lambda t: t.trigger_time)
        return tasks
    
    def get_task(self, task_id: str) -> Optional[WakeTask]:
        """获取指定任务"""
        return self._tasks.get(task_id)
    
    def _load_tasks_sync(self) -> List[dict]:
        """同步加载任务数据（供 run_in_executor 使用）"""
        if not os.path.exists(self.data_file):
            return []
        
        with open(self.data_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    async def _load_tasks(self):
        """从文件加载任务数据（异步包装）"""
        if not os.path.exists(self.data_file):
            return
        
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._load_tasks_sync)
            
            for task_data in data:
                task = WakeTask.from_dict(task_data)
                # 只加载未触发的任务
                if not task.triggered:
                    self._tasks[task.task_id] = task
            
            logger.debug(f"Loaded {len(self._tasks)} wake tasks from file")
        except Exception as e:
            logger.error(f"Failed to load wake tasks: {e}")
    
    def _save_tasks_sync(self, data: List[dict]):
        """同步保存任务数据（供 run_in_executor 使用）"""
        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    async def _save_tasks(self):
        """保存任务数据到文件（异步包装）"""
        try:
            data = [task.to_dict() for task in self._tasks.values()]
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save_tasks_sync, data)
        except Exception as e:
            logger.error(f"Failed to save wake tasks: {e}")
    
    async def _schedule_all_pending_tasks(self):
        """调度所有待执行的任务"""
        now = time.time()
        expired_count = 0
        scheduled_count = 0
        
        for task in list(self._tasks.values()):
            if task.triggered:
                continue
            
            if task.trigger_time <= now:
                # 已到期的任务：立即触发
                expired_count += 1
                asyncio.create_task(self._trigger_task(task))
            else:
                # 未到期的任务：正常调度
                scheduled_count += 1
                self._schedule_task(task)
        
        if expired_count > 0:
            logger.info(f"Triggered {expired_count} expired wake tasks on startup")
        if scheduled_count > 0:
            logger.info(f"Scheduled {scheduled_count} pending wake tasks")
    
    def _schedule_task(self, task: WakeTask):
        """调度单个任务"""
        delay = task.remaining_seconds()
        
        async def delayed_trigger():
            await asyncio.sleep(delay)
            await self._trigger_task(task)
        
        scheduled_task = asyncio.create_task(delayed_trigger())
        self._scheduled_tasks[task.task_id] = scheduled_task
    
    async def _trigger_task(self, task: WakeTask):
        """触发唤醒任务"""
        async with self._lock:
            # 检查任务是否仍然有效
            if task.task_id not in self._tasks:
                return
            if task.triggered:
                return
            
            # 标记为已触发
            task.triggered = True
            
            # 从调度任务中移除
            if task.task_id in self._scheduled_tasks:
                del self._scheduled_tasks[task.task_id]
            
            # 从任务列表中移除
            del self._tasks[task.task_id]
            await self._save_tasks()
        
        logger.info(f"Triggering wake task: {task.task_id} for session {task.session_id}")
        
        # 调用唤醒回调
        if self._wake_callback:
            try:
                await self._wake_callback(task)
            except Exception as e:
                logger.error(f"Failed to execute wake callback for task {task.task_id}: {e}")
        else:
            logger.warning(f"No wake callback set for task {task.task_id}")
    
    async def terminate(self):
        """终止调度器，取消所有调度任务"""
        async with self._lock:
            for task in self._scheduled_tasks.values():
                task.cancel()
            self._scheduled_tasks.clear()
            
            # 保存当前状态
            await self._save_tasks()
        
        logger.info("WakeScheduler terminated")