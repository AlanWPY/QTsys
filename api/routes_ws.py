"""WebSocket实时推送 + 任务管理API"""
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from tasks.task_manager import TaskManager
from logging_config import get_logger

logger = get_logger("qtsys.api.ws")

router = APIRouter(tags=["tasks"])


# ===== 任务查询API =====

@router.get("/api/tasks")
async def list_tasks(limit: int = Query(default=20, le=50)):
    """获取任务列表"""
    tm = TaskManager.get_instance()
    return tm.list_tasks(limit)


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查询单个任务状态"""
    tm = TaskManager.get_instance()
    info = tm.get_task(task_id)
    if not info:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="任务不存在")
    return info.to_dict()


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务"""
    tm = TaskManager.get_instance()
    ok = tm.cancel_task(task_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="无法取消该任务")
    return {"message": "已取消"}


# ===== WebSocket实时推送 =====

@router.websocket("/ws/tasks")
async def ws_task_updates(websocket: WebSocket):
    """WebSocket连接 - 实时推送任务状态变更"""
    await websocket.accept()
    tm = TaskManager.get_instance()
    queue = tm.subscribe()
    logger.info("WebSocket客户端已连接")
    try:
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        logger.info("WebSocket客户端断开")
    except Exception:
        logger.exception("WebSocket异常")
    finally:
        tm.unsubscribe(queue)
