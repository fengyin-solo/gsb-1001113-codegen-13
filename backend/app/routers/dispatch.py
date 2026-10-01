"""防汛应急事件驱动分拨接口。

四类信号（水位告警 / 泵站排水离线 / 边坡偏离 / 道路巡查）在接入侧共用
同一套写法与管线，仅按来源挂不同适配器，避免每类页面各写一套升级判断。
升级结论由分拨器统一投影到防汛台账、排水设施清单、养护工程汇总。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import CommandPayload, DispatchAck, EntryPayload
from app.services.dispatch import (
    SOURCES,
    SOURCE_LABELS,
    DispatchConflict,
    DispatchValidation,
    dispatcher,
)

router = APIRouter(prefix="/api/dispatch", tags=["防汛事件分拨"])

SIGNAL_PATHS = {
    "water_level": "/signals/water-level",
    "pump_offline": "/signals/pump-offline",
    "slope_deviation": "/signals/slope-deviation",
    "road_patrol": "/signals/road-patrol",
}


def _handle(fn, *args, **kwargs) -> DispatchAck:
    try:
        return DispatchAck(**fn(*args, **kwargs))
    except DispatchValidation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DispatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/events")
def list_events(status: str | None = Query(default=None, description="进行中、已结束")) -> dict:
    """事件列表：指纹合并后的防汛事件及其升级结论、处置节点进度。"""
    events = dispatcher.list_events(status=status)
    return {"total": len(events), "items": events}


@router.get("/events/{event_id}")
def get_event(event_id: int) -> dict:
    """事件详情：升级链、合并的信号与按前置件展开的处置树。"""
    event = dispatcher.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"防汛事件 {event_id} 不存在或已归档")
    return event


def _ingest(source: str, payload: EntryPayload) -> DispatchAck:
    return _handle(dispatcher.ingest_signal, source, payload.values)


@router.post(SIGNAL_PATHS["water_level"], response_model=DispatchAck)
def ingest_water_level(payload: EntryPayload) -> DispatchAck:
    """接入水位告警（阈值类型可写超警戒/超保证/漫溢，自动映射初始级别）。"""
    return _ingest("water_level", payload)


@router.post(SIGNAL_PATHS["pump_offline"], response_model=DispatchAck)
def ingest_pump_offline(payload: EntryPayload) -> DispatchAck:
    """接入泵站排水离线告警，结论同步排水设施清单。"""
    return _ingest("pump_offline", payload)


@router.post(SIGNAL_PATHS["slope_deviation"], response_model=DispatchAck)
def ingest_slope_deviation(payload: EntryPayload) -> DispatchAck:
    """接入边坡偏离告警（位移量可自动定级），按指纹合并重复告警。"""
    return _ingest("slope_deviation", payload)


@router.post(SIGNAL_PATHS["road_patrol"], response_model=DispatchAck)
def ingest_road_patrol(payload: EntryPayload) -> DispatchAck:
    """接入道路巡查发现的积水/塌方险情。"""
    return _ingest("road_patrol", payload)


@router.post("/signals", response_model=DispatchAck)
def ingest_signal(payload: EntryPayload) -> DispatchAck:
    """统一信号入口：values 里带 source（编码或中文名）即可，与四类专口同管线。"""
    source = str(payload.values.pop("source", "")).strip()
    if not source:
        raise HTTPException(status_code=400, detail="统一入口需在 values 中提供 source")
    return _ingest(source, payload)


@router.post("/events/{event_id}/commands", response_model=DispatchAck)
def issue_command(event_id: int, payload: CommandPayload) -> DispatchAck:
    """下达指挥部最新命令；现场管制冲突时以命令为准，版本过期返回 409。"""
    values = payload.model_dump(exclude_none=True)
    expected_version = values.pop("expected_version", None)
    return _handle(dispatcher.issue_command, event_id, values, expected_version=expected_version)


@router.post("/events/{event_id}/tasks/{task_id}/actions", response_model=DispatchAck)
def run_task_action(event_id: int, task_id: int, payload: EntryPayload) -> DispatchAck:
    """处置节点启动/完成；前置件未完成会被拦下并说明在等哪个节点。"""
    action = str(payload.values.get("action") or "").strip()
    return _handle(dispatcher.run_task_action, event_id, task_id, action, payload.values)


@router.post("/events/{event_id}/close", response_model=DispatchAck)
def close_event(event_id: int) -> DispatchAck:
    """结束响应：处置树全部节点完成后才允许，历史升级链随之定格。"""
    return _handle(dispatcher.close_event, event_id)


@router.post("/events/{event_id}/replay", response_model=DispatchAck)
def replay_old_signal(event_id: int) -> DispatchAck:
    """重放一条更旧、更低级别的同指纹信号，验证不能降低已确认预警级别。"""
    return _handle(dispatcher.replay_demo, event_id)


@router.get("/sources")
def list_sources() -> dict:
    """四类信号来源与其专口路径，供前端分拨台渲染接入口。"""
    return {
        "items": [
            {"code": code, "label": SOURCE_LABELS[code], "path": SIGNAL_PATHS[code]}
            for code in SOURCES
        ]
    }
