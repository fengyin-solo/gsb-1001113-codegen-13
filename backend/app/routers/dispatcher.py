"""事件驱动分拨器接口：统一接入多源告警、按前置件展开处置树、裁决升级结论并同步三方台账。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, AlarmPayload, EntryPayload, PageResult
from app.services.dispatcher import DispatcherService

router = APIRouter(prefix="/api/dispatcher", tags=["事件分拨"])

service = DispatcherService()


@router.get("/sources")
def describe_sources() -> dict[str, Any]:
    """读取告警来源、定级序列与处置树模板，前端表单按这份元数据渲染。"""
    return service.describe_sources()


@router.get("/events", response_model=PageResult[dict])
def list_events(
    keyword: str | None = Query(default=None, description="按事件编号检索"),
    level: str | None = Query(default=None, description="蓝色、黄色、橙色、红色"),
    status: str | None = Query(default=None, description="处置中、已结束"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页列出分拨事件；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_events(keyword=keyword, level=level, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/events/{event_id}", response_model=dict)
def get_event(event_id: int) -> dict:
    """读取单个事件明细：含按前置件展开的处置树、升级链与三方台账同步记录。"""
    event = service.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_id} 不存在或已归档")
    return event


@router.post("/alarms", response_model=ActionResult)
def ingest_alarm(payload: AlarmPayload) -> ActionResult:
    """统一告警入口：水位、泵站、边坡、巡查都走这里；重复告警按事件指纹合并。"""
    event, message = service.ingest_alarm(payload.source_type, payload.values)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_event(int(event["id"])))


@router.post("/events/{event_id}/escalate", response_model=ActionResult)
def request_escalation(event_id: int, payload: EntryPayload) -> ActionResult:
    """请求把事件预警上调一级；与现场管制冲突时以指挥部最新命令为准。"""
    reason = str(payload.values.get("reason") or "").strip()
    event, message = service.request_escalation(event_id, reason)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_event(event_id))


@router.post("/events/{event_id}/tasks/{task_key}", response_model=ActionResult)
def complete_task(event_id: int, task_key: str) -> ActionResult:
    """推进处置树节点；前置件未完成的节点会被拦下并说明缺哪几项。"""
    event, message = service.complete_task(event_id, task_key)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=event)


@router.post("/events/{event_id}/close", response_model=ActionResult)
def close_event(event_id: int) -> ActionResult:
    """结束事件：升级链封存为历史结论，后续指挥部命令不再改写它。"""
    event, message = service.close_event(event_id)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_event(event_id))


@router.post("/commands", response_model=ActionResult)
def issue_command(payload: EntryPayload) -> ActionResult:
    """签发指挥部命令：同文号重复投递幂等忽略，在办事件按最新命令重新裁决。"""
    command, message = service.issue_command(payload.values)
    if command is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=command)


@router.get("/commands", response_model=PageResult[dict])
def list_commands(page: int = 1, size: int = 50) -> PageResult[dict]:
    """按命令序号倒序列出指挥部命令，最新命令排在最前。"""
    items = service.list_commands()
    start = max(page - 1, 0) * size
    return PageResult(items=items[start:start + size], total=len(items), page=page, size=size)
