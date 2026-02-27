"""后台任务管理器 - 内存级异步任务队列"""
import asyncio
import uuid
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional, Callable
from logging_config import get_logger

logger = get_logger("qtsys.tasks")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    task_id: str
    task_type: str  # backtest / gp_mine / optimize / walk_forward
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0  # 0.0 ~ 1.0
    message: str = ""
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "progress": round(self.progress, 2),
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.status == TaskStatus.COMPLETED and self.result is not None:
            d["result"] = self.result
        return d


class TaskManager:
    """全局任务管理器 - 单例模式"""

    _instance: Optional["TaskManager"] = None

    def __init__(self):
        self._tasks: dict[str, TaskInfo] = {}
        self._async_tasks: dict[str, asyncio.Task] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._max_tasks = 100  # 最多保留100个任务记录

    @classmethod
    def get_instance(cls) -> "TaskManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def submit(self, task_type: str, func: Callable, *args, **kwargs) -> str:
        """提交异步任务，立即返回task_id"""
        task_id = uuid.uuid4().hex[:12]
        info = TaskInfo(task_id=task_id, task_type=task_type)
        self._tasks[task_id] = info
        self._cleanup_old()

        async def _wrapper():
            info.status = TaskStatus.RUNNING
            info.started_at = time.time()
            info.message = "执行中..."
            await self._notify(info)
            try:
                result = await asyncio.to_thread(func, *args, **kwargs)
                info.status = TaskStatus.COMPLETED
                info.result = result
                info.progress = 1.0
                info.message = "已完成"
            except Exception as e:
                info.status = TaskStatus.FAILED
                info.error = str(e)
                info.message = "执行失败"
                logger.exception(f"任务 {task_id} 失败")
            finally:
                info.finished_at = time.time()
                await self._notify(info)

        self._async_tasks[task_id] = asyncio.create_task(_wrapper())
        logger.info(f"任务已提交: {task_id} ({task_type})")
        return task_id

    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 20) -> list[dict]:
        tasks = sorted(
            self._tasks.values(),
            key=lambda t: t.created_at, reverse=True,
        )
        return [t.to_dict() for t in tasks[:limit]]

    def cancel_task(self, task_id: str) -> bool:
        info = self._tasks.get(task_id)
        if not info or info.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return False
        at = self._async_tasks.get(task_id)
        if at and not at.done():
            at.cancel()
        info.status = TaskStatus.CANCELLED
        info.finished_at = time.time()
        info.message = "已取消"
        return True

    def subscribe(self) -> asyncio.Queue:
        """订阅任务状态变更通知 (用于WebSocket推送)"""
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _notify(self, info: TaskInfo):
        """向所有订阅者推送任务状态"""
        msg = info.to_dict()
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    def _cleanup_old(self):
        """清理超出上限的已完成任务"""
        if len(self._tasks) <= self._max_tasks:
            return
        finished = [
            (tid, t) for tid, t in self._tasks.items()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        finished.sort(key=lambda x: x[1].finished_at)
        remove_count = len(self._tasks) - self._max_tasks
        for tid, _ in finished[:remove_count]:
            del self._tasks[tid]
            self._async_tasks.pop(tid, None)
