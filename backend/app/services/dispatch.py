"""防汛应急事件驱动分拨器。

四类上游信号（水位告警、泵站排水离线、边坡偏离、道路巡查）先由各自适配器
归一成同一种写法，再进入统一管线：

    信号 → 事件指纹合并 → 事件版本累加 → 升级链（只升不降）
        → 前置件处置树展开 → 事务内投影到防汛台账/排水设施/养护工程

关键口径（不允许各页面自行判断，统一收在本模块）：

* 重复告警按 ``source|类型|位置|对象编号`` 计算指纹，合并进同一事件；
* 乱序重放的旧版本信号只累加次数，不进入升级链，绝不降低已确认预警级别；
* 预警升级与现场管制冲突时，管制结论以指挥部最新命令为准，命令早于已确认
  级别时也不能把确认级别压回去（历史事件维持此前升级链，已结束事件不可变）。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from typing import Any, Callable

from app.store import store

# ---------------------------------------------------------------------------
# 领域常量：来源、级别、处置剧本
# ---------------------------------------------------------------------------

LEVELS = ["蓝色", "黄色", "橙色", "红色"]
LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}

SOURCES = ("water_level", "pump_offline", "slope_deviation", "road_patrol")
SOURCE_LABELS = {
    "water_level": "水位告警",
    "pump_offline": "泵站排水离线",
    "slope_deviation": "边坡偏离",
    "road_patrol": "道路巡查",
}
SOURCE_BY_LABEL = {label: code for code, label in SOURCE_LABELS.items()}

TASK_TODO = "待启动"
TASK_DOING = "进行中"
TASK_DONE = "已完成"
EVENT_OPEN = "进行中"
EVENT_CLOSED = "已结束"

# 每个来源的处置树模板：min_level 表示该节点需要事件达到的最低预警级别
# （0=蓝 1=黄 2=橙 3=红），requires 是必须先完成的前置件 code。
PLAYBOOK: dict[str, list[dict[str, Any]]] = {
    "water_level": [
        {"code": "alert", "name": "发布水情预警", "requires": [], "min_level": 0},
        {"code": "inspect", "name": "现场核查水位并值守", "requires": ["alert"], "min_level": 0},
        {"code": "drain", "name": "泵站预排、开闸强排", "requires": ["inspect"], "min_level": 0},
        {"code": "traffic", "name": "低洼路段交通管制", "requires": ["inspect"], "min_level": 2},
        {"code": "evac", "name": "受淹区域人员转移", "requires": ["traffic"], "min_level": 3},
    ],
    "pump_offline": [
        {"code": "alert", "name": "发布泵站离线告警", "requires": [], "min_level": 0},
        {"code": "switch", "name": "切换备用泵/移动泵车", "requires": ["alert"], "min_level": 0},
        {"code": "repair", "name": "机电抢修与供电恢复", "requires": ["switch"], "min_level": 0},
        {"code": "traffic", "name": "退水路径交通管制", "requires": ["switch"], "min_level": 2},
        {"code": "evac", "name": "影响区人员转移", "requires": ["traffic"], "min_level": 3},
    ],
    "slope_deviation": [
        {"code": "alert", "name": "发布边坡变形预警", "requires": [], "min_level": 0},
        {"code": "inspect", "name": "边坡裂缝与位移复核", "requires": ["alert"], "min_level": 0},
        {"code": "cover", "name": "覆盖彩条布、截排水", "requires": ["inspect"], "min_level": 0},
        {"code": "reinforce", "name": "反压护坡/抗滑桩加固", "requires": ["cover"], "min_level": 2},
        {"code": "evac", "name": "坡脚住户与人员转移", "requires": ["reinforce"], "min_level": 3},
    ],
    "road_patrol": [
        {"code": "alert", "name": "发布路况险情预警", "requires": [], "min_level": 0},
        {"code": "inspect", "name": "现场核查积水/塌方范围", "requires": ["alert"], "min_level": 0},
        {"code": "barricade", "name": "设置围挡、抽排积水", "requires": ["inspect"], "min_level": 0},
        {"code": "traffic", "name": "封闭交通并组织绕行", "requires": ["barricade"], "min_level": 2},
        {"code": "evac", "name": "被困车辆与人员疏散", "requires": ["traffic"], "min_level": 3},
    ],
}

DEFAULT_INCIDENT = {
    "water_level": "河道水位超警",
    "pump_offline": "泵站排水离线",
    "slope_deviation": "边坡位移超限",
    "road_patrol": "道路积水险情",
}
METRIC_LABELS = {
    "water_level": "水位(m)",
    "pump_offline": "离线时长(分钟)",
    "slope_deviation": "位移量(mm)",
    "road_patrol": "积水深度(cm)",
}


class DispatchConflict(RuntimeError):
    """事件版本不匹配、命令乱序等需要 409 返回的冲突。"""


class DispatchValidation(RuntimeError):
    """信号缺字段、前置件未满足等需要 400 返回的校验错误。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _first(values: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_level(value: Any, default: str = "蓝色") -> str:
    """把「红/红色/I 级」等写法归一到四色级别。"""
    text = _text(value)
    for name in LEVELS:
        if text.startswith(name) or text == name[0]:
            return name
    return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 信号适配器：四类来源归一成同一种写法
# ---------------------------------------------------------------------------

def _adapt_water_level(values: dict[str, Any]) -> dict[str, Any]:
    gauge = _text(_first(values, ["阈值类型", "告警类型", "incident_type"]))
    mapped = "蓝色"
    if "漫溢" in gauge or "漫堤" in gauge:
        mapped = "红色"
    elif "保证" in gauge:
        mapped = "橙色"
    elif "警戒" in gauge:
        mapped = "黄色"
    return {
        "source": "water_level",
        "source_ref": _text(_first(values, ["水位计编号", "设施编号", "source_ref"])),
        "location": _text(_first(values, ["影响路段", "所属路段", "位置", "location"])),
        # 指纹只用固定事件类别，阈值升级（警戒→保证→漫溢）进描述，不拆事件
        "incident_type": DEFAULT_INCIDENT["water_level"],
        "level": normalize_level(_first(values, ["预警级别", "级别", "level"]), mapped),
        "measure": _text(_first(values, ["水位", "当前水位", "measure"])),
        "description": _text(_first(values, ["告警描述", "描述", "remark"]))
        or (f"{gauge}，当前水位 {_text(_first(values, ['水位', '当前水位', 'measure']))}" if gauge else ""),
    }


def _adapt_pump_offline(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "pump_offline",
        "source_ref": _text(_first(values, ["泵站编号", "设施编号", "source_ref"])),
        "location": _text(_first(values, ["所属路段", "影响路段", "位置", "location"])),
        "incident_type": DEFAULT_INCIDENT["pump_offline"],
        "level": normalize_level(_first(values, ["预警级别", "级别", "level"]), "橙色"),
        "measure": _text(_first(values, ["离线时长", "时长", "measure"])),
        "description": _text(_first(values, ["告警描述", "描述", "remark"])),
    }


def _adapt_slope_deviation(values: dict[str, Any]) -> dict[str, Any]:
    raw = _first(values, ["偏离量", "位移量", "measure"])
    mapped = "蓝色"
    try:
        deviation = float(raw)
        if deviation >= 50:
            mapped = "橙色"
        elif deviation >= 20:
            mapped = "黄色"
    except (TypeError, ValueError):
        deviation = None
    return {
        "source": "slope_deviation",
        "source_ref": _text(_first(values, ["边坡编号", "source_ref"])),
        "location": _text(_first(values, ["所属路段", "影响路段", "位置", "location"])),
        "incident_type": DEFAULT_INCIDENT["slope_deviation"],
        "level": normalize_level(_first(values, ["预警级别", "级别", "level"]), mapped),
        "measure": _text(raw),
        "description": _text(_first(values, ["告警描述", "描述", "remark"]))
        or (f"位移量 {_text(raw)}mm，达{mapped}响应" if deviation is not None else ""),
    }


def _adapt_road_patrol(values: dict[str, Any]) -> dict[str, Any]:
    danger = _text(_first(values, ["险情类型", "告警类型", "incident_type"]))
    mapped = "黄色"
    if any(word in danger for word in ("塌方", "漫水", "淹没")):
        mapped = "橙色"
    return {
        "source": "road_patrol",
        "source_ref": _text(_first(values, ["巡查编号", "source_ref"])),
        "location": _text(_first(values, ["巡查路段", "影响路段", "所属路段", "位置", "location"])),
        "incident_type": DEFAULT_INCIDENT["road_patrol"],
        "level": normalize_level(_first(values, ["预警级别", "级别", "险情等级", "level"]), mapped),
        "measure": _text(_first(values, ["积水深度", "measure"])),
        "description": _text(_first(values, ["险情描述", "告警描述", "描述", "remark"]))
        or (danger if danger else ""),
    }


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "water_level": _adapt_water_level,
    "pump_offline": _adapt_pump_offline,
    "slope_deviation": _adapt_slope_deviation,
    "road_patrol": _adapt_road_patrol,
}


def normalize_signal(source: str, values: dict[str, Any]) -> dict[str, Any]:
    """入口归一：source 支持编码或中文名，补齐版本号与发生时间。"""
    code = SOURCE_BY_LABEL.get(_text(source), _text(source))
    if code not in ADAPTERS:
        raise DispatchValidation(f"未知信号来源「{source}」，支持：{ '、'.join(SOURCE_LABELS.values()) }")
    signal = ADAPTERS[code](values)
    if not signal["source_ref"] and not signal["location"]:
        raise DispatchValidation("信号至少要带对象编号或位置，无法计算事件指纹")
    try:
        signal["version"] = int(_first(values, ["source_version", "信号版本", "版本", "version"]) or 1)
    except (TypeError, ValueError) as exc:
        raise DispatchValidation("信号版本号必须是整数") from exc
    signal["occurred_at"] = _text(_first(values, ["发生时间", "occurred_at"])) or _now()
    return signal


def _fingerprint(signal: dict[str, Any]) -> str:
    basis = "|".join([
        signal["source"],
        signal["incident_type"],
        signal["location"],
        signal["source_ref"],
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 分拨器主体
# ---------------------------------------------------------------------------

class DispatchService:
    """事件聚合、升级链、处置树与跨模块投影都走这里的事务入口。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._seq = 0
        self._event_identity = 0
        self._booted = False

    # ---- 通用读取 --------------------------------------------------------

    def list_events(self, status: str | None = None) -> list[dict[str, Any]]:
        events = self.events if not status else [e for e in self.events if e["status"] == status]
        return [self.event_detail(event) for event in events]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        for event in self.events:
            if event["id"] == event_id:
                return self.event_detail(event)
        return None

    def _require_open_event(self, event_id: int) -> dict[str, Any]:
        event = next((item for item in self.events if item["id"] == event_id), None)
        if event is None:
            raise DispatchValidation(f"防汛事件 {event_id} 不存在或已归档")
        if event["status"] != EVENT_OPEN:
            raise DispatchValidation(f"事件 {event['编号']} 已结束，历史升级链维持原状不可再变更")
        return event

    def event_detail(self, event: dict[str, Any]) -> dict[str, Any]:
        """带处置树实时展开状态的只读视图：前置件是否满足在这里统一算。"""
        detail = copy.deepcopy(event)
        done = {task["code"] for task in event["tasks"] if task["status"] == TASK_DONE}
        for task in detail["tasks"]:
            waiting = [code for code in task["requires"]
                       if code not in done or not any(t["code"] == code for t in detail["tasks"])]
            task["前置件"] = "、".join(task["requires"]) if task["requires"] else "无"
            task["可启动"] = (
                task["status"] == TASK_TODO
                and not waiting
                and event["status"] == EVENT_OPEN
            )
            task["阻断原因"] = "需先完成：" + "、".join(waiting) if waiting else ""
        detail["来源"] = "、".join(SOURCE_LABELS[code] for code in event["sources"])
        detail["升级结论"] = self._effective_level(event)
        detail["待处置节点"] = sum(1 for t in event["tasks"] if t["status"] != TASK_DONE)
        return detail

    # ---- 级别口径 --------------------------------------------------------

    @staticmethod
    def _effective_level(event: dict[str, Any]) -> str:
        """事件升级结论：确认级别与指挥部命令取高者，任何一方都不能压低结论。"""
        rank = LEVEL_RANK[event["confirmed_level"]]
        command_level = event.get("command_level")
        if command_level:
            rank = max(rank, LEVEL_RANK[command_level])
        return LEVELS[rank]

    # ---- 信号接入（统一管线） --------------------------------------------

    def ingest_signal(self, source: str, values: dict[str, Any]) -> dict[str, Any]:
        signal = normalize_signal(source, values)
        fingerprint = _fingerprint(signal)
        with store.transaction():
            snapshot = copy.deepcopy(self.events)
            try:
                return self._ingest_locked(signal, fingerprint)
            except Exception:
                # 与 store 的事务快照配套：投影表回滚，事件状态一并回滚
                self.events = snapshot
                raise

    def _ingest_locked(self, signal: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        self._seq += 1
        seq = self._seq

        open_event = next(
            (event for event in self.events
             if event["fingerprint"] == fingerprint and event["status"] == EVENT_OPEN),
            None,
        )
        closed_event = next(
            (event for event in self.events if event["fingerprint"] == fingerprint),
            None,
        )

        # 已结束事件命中同指纹：历史链不可变，新告警另立事件
        if open_event is None and closed_event is not None:
            return {
                "ok": True,
                "message": f"信号命中已结束事件 {closed_event['编号']}，维持其历史升级链不做合并，已另立新事件",
                "event": self._create_event(signal, fingerprint, seq),
                "merged": False,
                "replay": False,
                "historical": closed_event["编号"],
            }

        if open_event is None:
            event = self._create_event(signal, fingerprint, seq)
            return {
                "ok": True,
                "message": f"新防汛事件 {event['编号']} 已建立，处置树按前置件展开",
                "event": self.event_detail(event),
                "merged": False,
                "replay": False,
            }

        return self._merge_signal(open_event, signal, seq)

    def _create_event(self, signal: dict[str, Any], fingerprint: str, seq: int) -> dict[str, Any]:
        self._event_identity += 1
        event_id = self._event_identity
        event: dict[str, Any] = {
            "id": event_id,
            "编号": f"EMRG-{event_id:04d}",
            "fingerprint": fingerprint,
            "sources": [signal["source"]],
            "primary_source": signal["source"],
            "source_ref": signal["source_ref"],
            "location": signal["location"] or "未定位路段",
            "incident_type": signal["incident_type"],
            "status": EVENT_OPEN,
            "confirmed_level": signal["level"],
            "command_level": None,
            "command_seq": None,
            "command_id": None,
            "管制结论": "待指挥部命令",
            "version": 1,
            "first_seen_at": signal["occurred_at"],
            "last_signal_at": signal["occurred_at"],
            "last_signal_seq": seq,
            "closed_at": None,
            "occurrences": 1,
            "max_signal_version": signal["version"],
            "signals": [self._signal_record(signal, seq, merged=False, replay=False)],
            "chain": [{
                "seq": seq,
                "kind": "signal",
                "at": signal["occurred_at"],
                "action": "事件定级",
                "level": signal["level"],
                "basis": f"{SOURCE_LABELS[signal['source']]} {signal['source_ref'] or signal['location']} 首次告警",
            }],
            "tasks": [],
            "task_seq": 0,
            "reached_orange_at": _today() if LEVEL_RANK[signal["level"]] >= 2 else None,
        }
        self._expand_tasks(event)
        self.events.append(event)
        self._sync_projections(event)
        return self.event_detail(event)

    def _merge_signal(self, event: dict[str, Any], signal: dict[str, Any], seq: int) -> dict[str, Any]:
        replay = signal["version"] <= event["max_signal_version"]
        record = self._signal_record(signal, seq, merged=True, replay=replay)
        event["signals"].append(record)
        event["occurrences"] += 1
        event["last_signal_at"] = signal["occurred_at"]
        event["version"] += 1

        if signal["source"] not in event["sources"]:
            event["sources"].append(signal["source"])

        message = f"重复告警已按事件指纹合并到 {event['编号']}（第 {event['occurrences']} 次）"

        if replay:
            # 乱序消息重放：只留痕，不进升级链，确认级别维持不动
            event["chain"].append({
                "seq": seq,
                "kind": "replay",
                "at": signal["occurred_at"],
                "action": "乱序重放已忽略降级",
                "level": signal["level"],
                "basis": f"信号版本 {signal['version']} ≤ 已处理版本 {event['max_signal_version']}",
            })
            self._sync_projections(event)
            return {
                "ok": True,
                "message": message + "；旧版本信号重放，已确认预警级别维持 "
                           f"{event['confirmed_level']} 未降级",
                "event": self.event_detail(event),
                "merged": True,
                "replay": True,
            }

        event["max_signal_version"] = signal["version"]
        event["last_signal_seq"] = seq
        before = event["confirmed_level"]
        if LEVEL_RANK[signal["level"]] > LEVEL_RANK[before]:
            # 自动升级只升不降
            event["confirmed_level"] = signal["level"]
            self._expand_tasks(event)
            if LEVEL_RANK[signal["level"]] >= 2 and not event.get("reached_orange_at"):
                event["reached_orange_at"] = _today()
            event["chain"].append({
                "seq": seq,
                "kind": "signal",
                "at": signal["occurred_at"],
                "action": "自动升级",
                "level": signal["level"],
                "basis": f"{before} → {signal['level']}（{SOURCE_LABELS[signal['source']]}版本 {signal['version']}）",
            })
            message += f"，预警自动升级为{signal['level']}"
        else:
            message += f"，已确认预警级别维持 {before}（未达升级条件）"

        self._sync_projections(event)
        return {
            "ok": True,
            "message": message,
            "event": self.event_detail(event),
            "merged": True,
            "replay": False,
        }

    @staticmethod
    def _signal_record(signal: dict[str, Any], seq: int, *, merged: bool, replay: bool) -> dict[str, Any]:
        return {
            "seq": seq,
            "source": signal["source"],
            "source_label": SOURCE_LABELS[signal["source"]],
            "source_ref": signal["source_ref"],
            "location": signal["location"],
            "level": signal["level"],
            "metric": METRIC_LABELS[signal["source"]],
            "measure": signal["measure"],
            "version": signal["version"],
            "occurred_at": signal["occurred_at"],
            "description": signal["description"],
            "merged": merged,
            "replay": replay,
        }

    # ---- 指挥部命令 ------------------------------------------------------

    def issue_command(
        self,
        event_id: int,
        values: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        level = normalize_level(values.get("级别") or values.get("level"), default="")
        if level not in LEVELS:
            raise DispatchValidation("指挥部命令需指定级别（蓝/黄/橙/红）")
        control = _text(values.get("管制动作") or values.get("control"))
        if not control:
            raise DispatchValidation("指挥部命令需给出管制动作（封闭交通/限速通行/解除管制等）")
        command_id = _text(values.get("命令编号") or values.get("command_id")) or f"CMD-{_today()}-{event_id}"
        try:
            command_seq = int(values.get("command_seq") or 0) or None
        except (TypeError, ValueError) as exc:
            raise DispatchValidation("命令序号必须是整数") from exc

        with store.transaction():
            snapshot = copy.deepcopy(self.events)
            try:
                event = self._require_open_event(event_id)
                if expected_version is not None and event["version"] != expected_version:
                    raise DispatchConflict(
                        f"事件版本已过期（期望 {expected_version}，当前 {event['version']}），请刷新后重下命令"
                    )
                if command_seq is not None and event["command_seq"] is not None \
                        and command_seq <= event["command_seq"]:
                    raise DispatchConflict(
                        f"命令序号 {command_seq} 不新于已执行的 {event['command_seq']}，乱序命令拒收"
                    )

                self._seq = max(self._seq + 1, command_seq or 0)
                seq = command_seq or self._seq
                old_level = self._effective_level(event)
                event["command_level"] = level
                event["command_seq"] = seq
                event["command_id"] = command_id
                event["管制结论"] = f"{control}（{command_id}）"
                event["version"] += 1
                event["chain"].append({
                    "seq": seq,
                    "kind": "command",
                    "at": _now(),
                    "action": f"指挥部命令·{control}",
                    "level": level,
                    "basis": f"命令编号 {command_id}（命令序号 {seq}）；现场管制以此最新命令为准",
                })
                self._sync_projections(event)
            except Exception:
                self.events = snapshot
                raise

        confirmed = event["confirmed_level"]
        note = ""
        if LEVEL_RANK[level] < LEVEL_RANK[confirmed]:
            note = f"；命令级别低于已确认的{confirmed}预警，确认级别不降级，仅现场管制按最新命令执行"
        return {
            "ok": True,
            "message": f"指挥部最新命令已生效，事件 {event['编号']} 升级结论 {old_level} → "
                       f"{self._effective_level(event)}，管制结论：{control}{note}",
            "event": self.event_detail(event),
        }

    # ---- 处置树 ----------------------------------------------------------

    def _expand_tasks(self, event: dict[str, Any]) -> None:
        """按当前级别展开前置件树；已存在的节点（含已完成）原样保留。"""
        rank = LEVEL_RANK[self._effective_level(event)]
        existing = {task["code"]: task for task in event["tasks"]}
        for template in PLAYBOOK[event["primary_source"]]:
            if template["code"] in existing:
                continue
            if template["min_level"] > rank:
                continue
            event["task_seq"] += 1
            existing[template["code"]] = {
                "id": event["task_seq"],
                "code": template["code"],
                "name": template["name"],
                "requires": list(template["requires"]),
                "min_level": template["min_level"],
                "status": TASK_TODO,
                "assignee": "",
                "result": "",
                "opened_at": _now(),
                "finished_at": "",
            }
            event["tasks"].append(existing[template["code"]])

    def run_task_action(
        self,
        event_id: int,
        task_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = values or {}
        with store.transaction():
            snapshot = copy.deepcopy(self.events)
            try:
                event = self._require_open_event(event_id)
                task = next((item for item in event["tasks"] if item["id"] == task_id), None)
                if task is None:
                    raise DispatchValidation(f"处置节点 {task_id} 不在事件 {event['编号']} 的处置树上")

                if action == "启动":
                    if task["status"] != TASK_TODO:
                        raise DispatchValidation(f"节点「{task['name']}」状态为{task['status']}，不能重复启动")
                    done = {item["code"] for item in event["tasks"] if item["status"] == TASK_DONE}
                    waiting = [code for code in task["requires"] if code not in done]
                    if waiting:
                        names = [self._task_name(event, code) for code in waiting]
                        raise DispatchValidation(
                            f"前置件未完成，节点「{task['name']}」暂不能启动，需先完成：{'、'.join(names)}"
                        )
                    task["status"] = TASK_DOING
                    task["assignee"] = _text(values.get("处置人")) or task["assignee"] or "值班抢险组"
                elif action == "完成":
                    if task["status"] != TASK_DOING:
                        raise DispatchValidation(f"节点「{task['name']}」尚未启动，不能直接完成")
                    task["status"] = TASK_DONE
                    task["result"] = _text(values.get("结果")) or "已按预案处置完毕"
                    task["finished_at"] = _now()
                else:
                    raise DispatchValidation(f"处置动作「{action}」不支持，仅可启动/完成")

                event["version"] += 1
                self._expand_tasks(event)
                self._sync_projections(event)
            except Exception:
                self.events = snapshot
                raise
        return {"ok": True, "message": f"节点「{task['name']}」已{action}", "event": self.event_detail(event)}

    @staticmethod
    def _task_name(event: dict[str, Any], code: str) -> str:
        for template in PLAYBOOK[event["primary_source"]]:
            if template["code"] == code:
                return template["name"]
        return code

    def close_event(self, event_id: int) -> dict[str, Any]:
        with store.transaction():
            snapshot = copy.deepcopy(self.events)
            try:
                event = self._require_open_event(event_id)
                unfinished = [task["name"] for task in event["tasks"] if task["status"] != TASK_DONE]
                if unfinished:
                    raise DispatchValidation(
                        f"处置树尚有 {len(unfinished)} 个节点未完成（{'、'.join(unfinished)}），不能结束响应"
                    )
                event["status"] = EVENT_CLOSED
                event["closed_at"] = _now()
                event["version"] += 1
                event["chain"].append({
                    "seq": self._seq,
                    "kind": "system",
                    "at": event["closed_at"],
                    "action": "结束响应",
                    "level": self._effective_level(event),
                    "basis": "处置树全部节点完成，历史升级链定格",
                })
                self._sync_projections(event)
            except Exception:
                self.events = snapshot
                raise
        return {"ok": True, "message": f"事件 {event['编号']} 已结束，升级链与处置记录归档", "event": self.event_detail(event)}

    # ---- 跨模块投影：升级结论唯一出口 ------------------------------------

    def _sync_projections(self, event: dict[str, Any]) -> None:
        """在同一事务里把结论写到防汛台账、排水设施清单、养护工程汇总。

        各页面只读取投影字段，不得自行推断级别/管制结论。
        """
        self._sync_flood_row(event)
        self._sync_drainage_row(event)
        self._sync_project_row(event)

    @staticmethod
    def _projection_row(table: str, event_id: int) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(table) if row.get("dispatch_event_id") == event_id),
            None,
        )

    @staticmethod
    def _next_row_id(table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1

    def _conclusion_fields(self, event: dict[str, Any]) -> dict[str, str]:
        return {
            "事件级别": self._effective_level(event),
            "管制结论": event["管制结论"],
            "事件版本": str(event["version"]),
            "数据来源": "事件分拨器",
        }

    def _sync_flood_row(self, event: dict[str, Any]) -> None:
        table = "flood"
        row = self._projection_row(table, event["id"])
        if row is None:
            row = {"id": self._next_row_id(table), "dispatch_event_id": event["id"]}
            store.rows(table).append(row)
        done = [task for task in event["tasks"] if task["status"] == TASK_DONE]
        doing = [task for task in event["tasks"] if task["status"] == TASK_DOING]
        measures = []
        if done:
            measures.append("已完成：" + "、".join(task["name"] for task in done))
        if doing:
            measures.append("进行中：" + "、".join(task["name"] for task in doing))
        latest_water = next(
            (s for s in reversed(event["signals"]) if s["source"] in ("water_level", "road_patrol") and s["measure"]),
            None,
        )
        if event["status"] == EVENT_CLOSED:
            flood_status = "已结束"
        elif doing or done:
            flood_status = "响应中"
        else:
            flood_status = "待响应"
        conclusions = self._conclusion_fields(event)
        row.update({
            "status": flood_status,
            "pending": event["status"] == EVENT_OPEN,
            "abnormal": LEVEL_RANK[conclusions["事件级别"]] >= 2,
            "记录编号": f"EMRG-{event['id']:04d}",
            "事件编号": event["编号"],
            "预警级别": conclusions["事件级别"],
            "影响路段": event["location"],
            "积水深度": f"{latest_water['measure']}{latest_water['metric']}" if latest_water else "—",
            "应急措施": "；".join(measures) or "处置树已展开，等待启动首节点",
            "投入人员": f"处置节点 {len(done)}/{len(event['tasks'])}",
            "恢复时间": event["closed_at"] or "",
            "防汛状态": flood_status,
            "管制结论": event["管制结论"],
            "事件版本": conclusions["事件版本"],
            "数据来源": "事件分拨器",
        })

    def _sync_drainage_row(self, event: dict[str, Any]) -> None:
        table = "drainage"
        row = self._projection_row(table, event["id"])
        is_pump = "pump_offline" in event["sources"]
        if not is_pump:
            return
        conclusions = self._conclusion_fields(event)
        open_flag = event["status"] == EVENT_OPEN
        if row is None:
            row = {"id": self._next_row_id(table), "dispatch_event_id": event["id"]}
            store.rows(table).append(row)
        row.update({
            "status": "堵塞" if open_flag else "正常",
            "pending": open_flag,
            "abnormal": open_flag,
            "设施编号": event["source_ref"] or f"PUMP-{event['id']:04d}",
            "设施类型": "排水泵站",
            "所属路段": event["location"],
            "桩号位置": "—",
            "清理日期": event["first_seen_at"][:10],
            "淤积程度": "排水离线" if open_flag else "已恢复运行",
            "管养班组": "应急抢险队",
            "设施状态": f"泵站离线·{conclusions['事件级别']}" if open_flag else "运行正常",
            "关联事件": event["编号"],
            "事件级别": conclusions["事件级别"],
            "管制结论": conclusions["管制结论"],
            "事件版本": conclusions["事件版本"],
            "数据来源": "事件分拨器",
        })

    def _sync_project_row(self, event: dict[str, Any]) -> None:
        table = "project"
        row = self._projection_row(table, event["id"])
        level = self._effective_level(event)
        if LEVEL_RANK[level] < 2:
            # 未达橙级不立抢险工程；分拨器是唯一写入方，达级后不会再回落
            return
        open_flag = event["status"] == EVENT_OPEN
        if row is None:
            row = {
                "id": self._next_row_id(table),
                "dispatch_event_id": event["id"],
                "开工日期": event.get("reached_orange_at") or _today(),
            }
            store.rows(table).append(row)
        row.update({
            "status": "施工中" if open_flag else "已竣工",
            "pending": open_flag,
            "abnormal": open_flag,
            "工程编号": f"EMRG-P{event['id']:03d}",
            "工程名称": f"{event['incident_type']}应急抢修工程（{event['location']}）",
            "工程类型": "防汛应急抢修",
            "施工路段": event["location"],
            "承建单位": "市政应急抢险队",
            "竣工日期": event["closed_at"][:10] if event["closed_at"] else "",
            "工程状态": "施工中" if open_flag else "已竣工",
            "关联事件": event["编号"],
            "事件级别": level,
            "管制结论": event["管制结论"],
            "事件版本": str(event["version"]),
            "数据来源": "事件分拨器",
        })

    # ---- 重放演示：验证乱序消息不能降级 ----------------------------------

    def replay_demo(self, event_id: int) -> dict[str, Any]:
        """构造一条更旧、更低级别的同指纹信号重放，级别应保持不变。"""
        event = next((item for item in self.events if item["id"] == event_id), None)
        if event is None:
            raise DispatchValidation(f"防汛事件 {event_id} 不存在")
        if event["status"] != EVENT_OPEN:
            raise DispatchValidation(f"事件 {event['编号']} 已结束，仅可查看历史升级链")
        original = event["confirmed_level"]
        values = {
            "水位计编号": event["source_ref"] if event["primary_source"] == "water_level" else "",
            "泵站编号": event["source_ref"] if event["primary_source"] == "pump_offline" else "",
            "边坡编号": event["source_ref"] if event["primary_source"] == "slope_deviation" else "",
            "巡查编号": event["source_ref"] if event["primary_source"] == "road_patrol" else "",
            "影响路段": event["location"],
            "告警类型": event["incident_type"],
            "预警级别": "蓝色",
            "source_version": 1,
            "发生时间": "2026-09-01 00:00:00",
            "remark": "模拟乱序重放的旧消息",
        }
        result = self.ingest_signal(event["primary_source"], values)
        result["before_level"] = original
        result["after_level"] = result["event"]["confirmed_level"]
        result["downgraded"] = LEVEL_RANK[result["after_level"]] < LEVEL_RANK[original]
        return result

    # ---- 演示数据：起服务即有一条展开中的事件链 --------------------------

    def ensure_demo(self) -> None:
        if self._booted:
            return
        self._booted = True
        # 事件一：水位告警（橙色，进行中），含一次同指纹重复告警合并
        first = self.ingest_signal("water_level", {
            "水位计编号": "WL-G07",
            "影响路段": "滨江南路低洼段",
            "阈值类型": "超保证水位",
            "水位": "4.82",
            "source_version": 3,
            "发生时间": "2026-09-30 21:10:00",
        })
        event_id = first["event"]["id"]
        self.ingest_signal("water_level", {
            "水位计编号": "WL-G07",
            "影响路段": "滨江南路低洼段",
            "阈值类型": "超保证水位",
            "水位": "4.85",
            "source_version": 3,
            "发生时间": "2026-09-30 21:15:00",
        })
        # 演示处置树进展：预警已完成，现场核查进行中
        self.run_task_action(event_id, 1, "启动")
        self.run_task_action(event_id, 1, "完成")
        self.run_task_action(event_id, 2, "启动", {"处置人": "王强"})

        # 事件二：泵站排水离线（橙色，进行中）
        self.ingest_signal("pump_offline", {
            "泵站编号": "PUMP-12",
            "所属路段": "河西北立交下穿道",
            "离线时长": "35",
            "发生时间": "2026-09-30 21:32:00",
        })

        # 事件三：历史道路巡查事件，黄→红升级链 + 指挥部命令，已结束并定格
        history = self.ingest_signal("road_patrol", {
            "巡查编号": "PATR-0928-03",
            "巡查路段": "老城区人民路下穿通道",
            "险情类型": "道路积水险情",
            "预警级别": "黄色",
            "积水深度": "18",
            "source_version": 1,
            "发生时间": "2026-09-28 14:05:00",
        })
        history_id = history["event"]["id"]
        self.ingest_signal("road_patrol", {
            "巡查编号": "PATR-0928-03",
            "巡查路段": "老城区人民路下穿通道",
            "险情类型": "道路积水险情",
            "预警级别": "红色",
            "积水深度": "46",
            "source_version": 2,
            "发生时间": "2026-09-28 14:40:00",
        })
        # 节点按前置件顺序滚动展开完成，直到没有可启动节点
        for _round in range(10):
            detail = self.event_detail(self.events[history_id - 1])
            ready = [task for task in detail["tasks"] if task["status"] == TASK_TODO and task["可启动"]]
            if not ready:
                break
            for task in ready:
                self.run_task_action(history_id, task["id"], "启动")
                self.run_task_action(history_id, task["id"], "完成")
        else:  # pragma: no cover - 处置树层级很浅，兜底防死循环
            raise RuntimeError("处置树滚动展开异常")
        self.issue_command(history_id, {
            "级别": "红色",
            "管制动作": "封闭交通",
            "命令编号": "CMD-20260928-07",
        })
        # 指挥部随后解除管制：管制结论更新，红色确认级别不降级
        self.issue_command(history_id, {
            "级别": "蓝色",
            "管制动作": "解除管制",
            "命令编号": "CMD-20260928-09",
        })
        self.close_event(history_id)


dispatcher = DispatchService()
