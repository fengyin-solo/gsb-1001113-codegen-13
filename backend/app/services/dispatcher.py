"""防汛应急事件驱动分拨器。

把水位告警、泵站排水离线、边坡偏离、道路巡查四类来源收成统一告警写法，
转换为事件并按前置件关系展开处置树；事件升级结论由这里统一裁决后，
在同一事务里同步到防汛台账、排水设施清单和养护工程汇总，页面只读不判。

裁决规则：
- 重复告警按事件指纹合并，同一告警编号重复投递幂等忽略；
- 预警升级与现场管制冲突时以指挥部最新命令为准，已结束的历史事件维持此前升级链；
- 跨模块写入携带事件版本并包在事务里，乱序消息重放不能降低已确认预警级别。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.store import store

EVENT_TABLE = "dispatcher_event"
COMMAND_TABLE = "dispatcher_command"
LEDGER_MODULES = ("flood", "drainage", "project")

LEVEL_ORDER = ["蓝色", "黄色", "橙色", "红色"]
COMMAND_TYPES = ["现场管制", "解除管制", "调整级别"]
EVENT_OPEN = "处置中"
EVENT_CLOSED = "已结束"

# 统一告警写法：每种来源声明必填字段、路段/设施取数路径与默认定级规则，
# 接入层只面对同一种告警结构，新增来源时在这里登记即可。
SOURCE_TYPES: dict[str, dict[str, Any]] = {
    "water_level": {
        "label": "水位告警",
        "category": "积水",
        "required": ["监测点", "影响路段", "当前水位", "警戒水位"],
        "road_field": "影响路段",
        "facility_field": "监测点",
    },
    "pump_offline": {
        "label": "泵站排水离线",
        "category": "排水",
        "required": ["泵站编号", "影响路段", "离线时长"],
        "road_field": "影响路段",
        "facility_field": "泵站编号",
    },
    "slope_deviation": {
        "label": "边坡偏离",
        "category": "边坡",
        "required": ["边坡编号", "所属路段", "位移量"],
        "road_field": "所属路段",
        "facility_field": "边坡编号",
    },
    "road_patrol": {
        "label": "道路巡查",
        "category": "巡查",
        "required": ["巡查编号", "巡查路段", "发现问题"],
        "road_field": "巡查路段",
        "facility_field": "巡查编号",
    },
}

# 处置树模板：requires 即前置件，展开时按拓扑序排列，前置件未完成不许推进。
DISPOSAL_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "water_level": [
        {"key": "survey", "title": "现场勘查积水范围", "requires": []},
        {"key": "pump", "title": "启动泵站强排", "requires": ["survey"]},
        {"key": "block", "title": "积水路段临时封控", "requires": ["survey"]},
        {"key": "transfer", "title": "转移受威胁人员车辆", "requires": ["block"]},
        {"key": "recheck", "title": "退水后复查排水设施", "requires": ["pump"]},
        {"key": "archive", "title": "复盘归档", "requires": ["transfer", "recheck"]},
    ],
    "pump_offline": [
        {"key": "inspect", "title": "核查泵站离线原因", "requires": []},
        {"key": "repair", "title": "组织抢修恢复排水", "requires": ["inspect"]},
        {"key": "standby", "title": "调派移动泵车兜底", "requires": ["inspect"]},
        {"key": "verify", "title": "复线后验证排水能力", "requires": ["repair", "standby"]},
    ],
    "slope_deviation": [
        {"key": "monitor", "title": "布设位移监测点", "requires": []},
        {"key": "cordon", "title": "坡脚警戒封控", "requires": ["monitor"]},
        {"key": "reinforce", "title": "应急支护加固", "requires": ["cordon"]},
        {"key": "assess", "title": "稳定性复评", "requires": ["reinforce"]},
    ],
    "road_patrol": [
        {"key": "confirm", "title": "现场确认问题", "requires": []},
        {"key": "dispatch", "title": "派单处置", "requires": ["confirm"]},
        {"key": "followup", "title": "复查销号", "requires": ["dispatch"]},
    ],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rank(level: str) -> int:
    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else -1


def _to_float(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _fingerprint(alarm: dict[str, Any]) -> str:
    """事件指纹：同来源、同路段、同设施、同类别的告警归并到同一事件。"""
    basis = "|".join([alarm["source_type"], alarm["路段"], alarm["设施"], alarm["类别"]])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


class DispatcherService:
    # ------------------------------------------------------------------
    # 告警接入：统一写法 → 归一化告警 → 事件
    # ------------------------------------------------------------------
    def ingest_alarm(
        self, source_type: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        if source_type not in SOURCE_TYPES:
            return None, f"未知告警来源「{source_type}」，可选：{'、'.join(SOURCE_TYPES)}"
        alarm, error = self._normalize(source_type, values)
        if alarm is None:
            return None, error
        with store.transaction(EVENT_TABLE, *LEDGER_MODULES):
            fingerprint = _fingerprint(alarm)
            event = self._find_open_event(fingerprint)
            if event is None:
                event = self._create_event(alarm, fingerprint)
                store.rows(EVENT_TABLE).append(event)
                self._apply_conclusion(event, reason=f"{alarm['来源']}首次上报")
                return event, f"已生成事件 {event['事件编号']}，结论同步到防汛台账、排水设施、养护工程"
            if alarm["alarm_id"] in event["alarm_ids"]:
                return event, f"告警 {alarm['alarm_id']} 已投递过，按事件指纹幂等忽略"
            out_of_order = alarm["occurred_at"] < event["last_alarm_at"]
            event["alarm_ids"].append(alarm["alarm_id"])
            event["alarms"].append({**alarm, "out_of_order": out_of_order})
            event["alarm_count"] += 1
            if out_of_order:
                # 乱序重放：只留痕不参与定级，已确认预警级别不受影响
                return event, f"乱序告警已登记到 {event['事件编号']}，已确认级别维持{event['confirmed_level']}"
            event["last_alarm_at"] = alarm["occurred_at"]
            if _rank(alarm["suggested_level"]) > _rank(event["auto_level"]):
                event["auto_level"] = alarm["suggested_level"]
                self._apply_conclusion(event, reason=f"{alarm['来源']}升级上报")
                return event, f"重复告警按指纹合并到 {event['事件编号']} 并触发升级"
            return event, f"重复告警按指纹合并到 {event['事件编号']}"

    def _normalize(
        self, source_type: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        spec = SOURCE_TYPES[source_type]
        missing = [f for f in spec["required"] if not str(values.get(f) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        occurred_at = str(values.get("发生时间") or "").strip() or _now()
        road = str(values.get(spec["road_field"]) or "").strip()
        facility = str(values.get(spec["facility_field"]) or "").strip()
        suggested, level_error = self._suggest_level(source_type, values)
        if level_error:
            return None, level_error
        alarm_id = str(values.get("告警编号") or "").strip() or f"{source_type}-{facility}-{occurred_at}"
        return {
            "alarm_id": alarm_id,
            "source_type": source_type,
            "来源": spec["label"],
            "路段": road,
            "设施": facility,
            "类别": spec["category"],
            "suggested_level": suggested,
            "detail": self._describe(source_type, values),
            "occurred_at": occurred_at,
        }, ""

    def _suggest_level(
        self, source_type: str, values: dict[str, Any]
    ) -> tuple[str, str]:
        explicit = str(values.get("建议级别") or "").strip()
        if explicit:
            if explicit not in LEVEL_ORDER:
                return "", f"建议级别「{explicit}」不在允许序列：{'、'.join(LEVEL_ORDER)}"
            return explicit, ""
        if source_type == "water_level":
            current = _to_float(values.get("当前水位"))
            warning = _to_float(values.get("警戒水位"))
            if current is None or warning in (None, 0):
                return "黄色", ""
            ratio = current / warning
            return ("橙色" if ratio >= 1.2 else "黄色" if ratio >= 1.0 else "蓝色"), ""
        if source_type == "pump_offline":
            hours = _to_float(values.get("离线时长"))
            if hours is None:
                return "黄色", ""
            return ("橙色" if hours >= 4 else "黄色" if hours >= 1 else "蓝色"), ""
        if source_type == "slope_deviation":
            shift = _to_float(values.get("位移量"))
            if shift is None:
                return "黄色", ""
            return ("红色" if shift >= 50 else "橙色" if shift >= 20 else "黄色" if shift > 0 else "蓝色"), ""
        text = str(values.get("发现问题") or "")
        if any(word in text for word in ("塌方", "滑坡", "淹没")):
            return "橙色", ""
        if any(word in text for word in ("积水", "坑槽", "堵")):
            return "黄色", ""
        return "蓝色", ""

    def _describe(self, source_type: str, values: dict[str, Any]) -> str:
        if source_type == "water_level":
            return f"当前水位{values.get('当前水位')}，警戒水位{values.get('警戒水位')}"
        if source_type == "pump_offline":
            return f"泵站离线{values.get('离线时长')}小时"
        if source_type == "slope_deviation":
            return f"边坡位移{values.get('位移量')}mm"
        return str(values.get("发现问题") or "")

    # ------------------------------------------------------------------
    # 事件查询
    # ------------------------------------------------------------------
    def list_events(
        self,
        *,
        keyword: str | None = None,
        level: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows(EVENT_TABLE), key=lambda row: int(row.get("id", 0)), reverse=True)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("事件编号", ""))]
        if level:
            rows = [row for row in rows if row.get("level") == level]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._summary(row) for row in rows[start:start + size]], total

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        event = store.find(EVENT_TABLE, event_id)
        if event is None:
            return None
        detail = dict(event)
        detail["disposal_tree"] = self._tree_with_state(event)
        return detail

    def _summary(self, event: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id", "事件编号", "来源", "路段", "设施", "类别", "level", "confirmed_level",
            "auto_level", "status", "version", "alarm_count", "updated_at",
        )
        return {key: event.get(key) for key in keys}

    def _tree_with_state(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = event["disposal_tree"]
        done = {node["key"] for node in nodes if node["completed"]}
        titled = {node["key"]: node["title"] for node in nodes}
        tree = []
        for node in nodes:
            unmet = [titled[key] for key in node["requires"] if key not in done]
            state = "已完成" if node["completed"] else ("待前置" if unmet else "可执行")
            tree.append({**node, "state": state, "unmet": unmet,
                         "requires_titles": [titled[key] for key in node["requires"]]})
        return tree

    # ------------------------------------------------------------------
    # 处置树推进：前置件未完成一律拦下
    # ------------------------------------------------------------------
    def complete_task(self, event_id: int, task_key: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction(EVENT_TABLE):
            event = store.find(EVENT_TABLE, event_id)
            if event is None:
                return None, f"事件 {event_id} 不存在或已归档"
            if event["status"] == EVENT_CLOSED:
                return None, f"事件 {event['事件编号']} 已结束，处置树不再推进"
            nodes = {node["key"]: node for node in event["disposal_tree"]}
            node = nodes.get(task_key)
            if node is None:
                return None, f"处置节点「{task_key}」不在事件 {event['事件编号']} 的处置树里"
            if node["completed"]:
                return self.get_event(event_id), f"节点「{node['title']}」此前已完成"
            unmet = [nodes[key]["title"] for key in node["requires"] if not nodes[key]["completed"]]
            if unmet:
                return None, f"前置件未完成：{'、'.join(unmet)}"
            node["completed"] = True
            node["completed_at"] = _now()
            event["version"] += 1
            event["updated_at"] = _now()
            return self.get_event(event_id), f"节点「{node['title']}」已完成"

    # ------------------------------------------------------------------
    # 升级裁决与指挥部命令
    # ------------------------------------------------------------------
    def request_escalation(self, event_id: int, reason: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction(EVENT_TABLE, *LEDGER_MODULES):
            event = store.find(EVENT_TABLE, event_id)
            if event is None:
                return None, f"事件 {event_id} 不存在或已归档"
            if event["status"] == EVENT_CLOSED:
                return None, f"事件 {event['事件编号']} 已结束，升级链已封存"
            if _rank(event["auto_level"]) >= len(LEVEL_ORDER) - 1:
                return None, f"事件 {event['事件编号']} 已是最高级别{LEVEL_ORDER[-1]}"
            event["auto_level"] = LEVEL_ORDER[_rank(event["auto_level"]) + 1]
            changed = self._apply_conclusion(event, reason=reason or "人工请求升级")
            if not changed:
                return event, f"指挥部命令管控中，生效级别维持{event['level']}"
            return event, f"事件 {event['事件编号']} 升级为{event['level']}，结论已同步三方台账"

    def issue_command(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [f for f in ("命令文号", "路段", "类型") if not str(values.get(f) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        command_type = str(values["类型"]).strip()
        if command_type not in COMMAND_TYPES:
            return None, f"命令类型「{command_type}」不在允许范围：{'、'.join(COMMAND_TYPES)}"
        level = str(values.get("管控级别") or "").strip()
        if command_type in ("现场管制", "调整级别"):
            if level not in LEVEL_ORDER:
                return None, f"管控级别需为：{'、'.join(LEVEL_ORDER)}"
        with store.transaction(COMMAND_TABLE, EVENT_TABLE, *LEDGER_MODULES):
            if any(row.get("命令文号") == values["命令文号"] for row in store.rows(COMMAND_TABLE)):
                return None, f"命令 {values['命令文号']} 已签发过，重复投递幂等忽略"
            seq = store.next_version("command_seq")
            command = {
                "id": max((int(row.get("id", 0)) for row in store.rows(COMMAND_TABLE)), default=0) + 1,
                "命令文号": str(values["命令文号"]).strip(),
                "路段": str(values["路段"]).strip(),
                "类型": command_type,
                "管控级别": level or None,
                "命令序号": seq,
                "发布人": str(values.get("发布人") or "指挥部").strip(),
                "issued_at": _now(),
            }
            store.rows(COMMAND_TABLE).append(command)
            affected = []
            for event in store.rows(EVENT_TABLE):
                # 已结束的历史事件维持此前升级链，不参与新命令裁决
                if event["status"] == EVENT_CLOSED or event["路段"] != command["路段"]:
                    continue
                if seq <= int(event.get("applied_command_seq", 0)):
                    continue  # 乱序命令重放不生效
                event["applied_command_seq"] = seq
                self._apply_conclusion(event, reason=f"指挥部命令{command['命令文号']}（{command_type}）")
                affected.append(event["事件编号"])
            command["affected_events"] = affected
            if affected:
                return command, f"命令{command['命令文号']}已下达，{len(affected)} 个在办事件按最新命令裁决"
            return command, f"命令{command['命令文号']}已下达，暂无在办事件受影响"

    def list_commands(self) -> list[dict[str, Any]]:
        return sorted(store.rows(COMMAND_TABLE), key=lambda row: int(row.get("命令序号", 0)), reverse=True)

    def close_event(self, event_id: int) -> tuple[dict[str, Any] | None, str]:
        with store.transaction(EVENT_TABLE):
            event = store.find(EVENT_TABLE, event_id)
            if event is None:
                return None, f"事件 {event_id} 不存在或已归档"
            if event["status"] == EVENT_CLOSED:
                return event, f"事件 {event['事件编号']} 此前已结束"
            event["status"] = EVENT_CLOSED
            event["pending"] = False
            event["version"] += 1
            event["updated_at"] = _now()
            return event, f"事件 {event['事件编号']} 已结束，升级链封存为历史结论"

    # ------------------------------------------------------------------
    # 内部：事件创建、结论裁决、三方台账同步
    # ------------------------------------------------------------------
    def _find_open_event(self, fingerprint: str) -> dict[str, Any] | None:
        for row in store.rows(EVENT_TABLE):
            if row.get("fingerprint") == fingerprint and row.get("status") == EVENT_OPEN:
                return row
        return None

    def _create_event(self, alarm: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        event_id = max((int(row.get("id", 0)) for row in store.rows(EVENT_TABLE)), default=0) + 1
        return {
            "id": event_id,
            "事件编号": f"EVNT-{event_id:04d}",
            "fingerprint": fingerprint,
            "source_type": alarm["source_type"],
            "来源": alarm["来源"],
            "路段": alarm["路段"],
            "设施": alarm["设施"],
            "类别": alarm["类别"],
            "level": alarm["suggested_level"],
            # 已确认级别从高水位线起步，由 _apply_conclusion 按实际同步的结论抬升
            "confirmed_level": LEVEL_ORDER[0],
            "auto_level": alarm["suggested_level"],
            "status": EVENT_OPEN,
            "pending": True,
            "abnormal": False,
            "version": 0,
            "alarm_count": 1,
            "alarm_ids": [alarm["alarm_id"]],
            "alarms": [{**alarm, "out_of_order": False}],
            "escalation_chain": [],
            "disposal_tree": self._expand_tree(alarm["source_type"]),
            "sync_log": [],
            "applied_command_seq": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "last_alarm_at": alarm["occurred_at"],
        }

    def _expand_tree(self, source_type: str) -> list[dict[str, Any]]:
        """按前置件关系把模板展开成拓扑有序的处置树；模板有环或悬空前置件直接报错。"""
        template = DISPOSAL_TEMPLATES[source_type]
        known = {node["key"] for node in template}
        for node in template:
            dangling = [key for key in node["requires"] if key not in known]
            if dangling:
                raise ValueError(f"处置树模板引用了不存在的前置件：{'、'.join(dangling)}")
        ordered: list[dict[str, Any]] = []
        done: set[str] = set()
        remaining = [dict(node, completed=False, completed_at=None) for node in template]
        while remaining:
            ready = [node for node in remaining if all(key in done for key in node["requires"])]
            if not ready:
                raise ValueError(f"处置树模板存在循环前置件：{source_type}")
            for node in ready:
                ordered.append(node)
                done.add(node["key"])
                remaining.remove(node)
        return ordered

    def _resolve_level(self, event: dict[str, Any]) -> tuple[str, str]:
        """裁决生效级别：预警升级与现场管制冲突时，以指挥部最新命令为准。"""
        commands = [row for row in store.rows(COMMAND_TABLE) if row.get("路段") == event["路段"]]
        if commands:
            latest = max(commands, key=lambda row: int(row.get("命令序号", 0)))
            if latest["类型"] in ("现场管制", "调整级别") and latest.get("管控级别"):
                return latest["管控级别"], f"指挥部命令{latest['命令文号']}"
        return event["auto_level"], "自动升级"

    def _apply_conclusion(self, event: dict[str, Any], *, reason: str) -> bool:
        """落定升级结论：升版本、记升级链、同事务同步三方台账。结论无变化则不产生新版本。"""
        effective, basis = self._resolve_level(event)
        if effective == event["level"] and event["escalation_chain"]:
            return False
        event["version"] += 1
        previous = event["level"]
        event["level"] = effective
        # 已确认级别是高水位线：只升不降，乱序重放拉不低
        if _rank(effective) > _rank(event["confirmed_level"]):
            event["confirmed_level"] = effective
        event["escalation_chain"].append({
            "version": event["version"],
            "from": previous,
            "to": effective,
            "basis": basis,
            "reason": reason,
            "at": _now(),
        })
        sync_result = self._sync_ledgers(event)
        event["sync_log"].append({"version": event["version"], "level": effective,
                                  "at": _now(), **sync_result})
        event["updated_at"] = _now()
        return True

    def _sync_ledgers(self, event: dict[str, Any]) -> dict[str, Any]:
        """把升级结论一次性写进防汛台账、排水设施清单、养护工程汇总。

        调用方必须已持有 store.transaction；任何一步失败，三方台账与事件版本一起回滚，
        页面只读取这里写入的联动结论，不各自判断。
        """
        conclusion = f"{event['level']}预警 · {event['事件编号']}"
        link = {"联动事件": event["事件编号"], "联动结论": conclusion, "联动版本": event["version"]}
        return {
            "flood": self._sync_flood(event, link),
            "drainage": self._sync_drainage(event, link),
            "project": self._sync_project(event, link),
        }

    def _sync_flood(self, event: dict[str, Any], link: dict[str, Any]) -> list[str]:
        rows = [row for row in store.rows("flood") if row.get("影响路段") == event["路段"]]
        if not rows:
            row = {
                "id": max((int(item.get("id", 0)) for item in store.rows("flood")), default=0) + 1,
                "记录编号": f"FLOO-{event['id']:04d}",
                "影响路段": event["路段"],
                "积水深度": "联动核查",
                "应急措施": "按处置树推进",
                "投入人员": "联动值守",
                "恢复时间": "",
            }
            store.rows("flood").append(row)
            rows = [row]
        for row in rows:
            row["预警级别"] = event["level"]
            row["防汛状态"] = "联动响应"
            if row.get("status") != "已结束":
                row["status"] = "响应中"
                row["pending"] = True
            row.update(link)
        return [str(row.get("记录编号")) for row in rows]

    def _sync_drainage(self, event: dict[str, Any], link: dict[str, Any]) -> list[str]:
        rows = [row for row in store.rows("drainage") if row.get("所属路段") == event["路段"]]
        for row in rows:
            row["淤积程度"] = "联动核查"
            row["设施状态"] = f"防汛{event['level']}联动"
            row["status"] = "淤积"
            row["pending"] = True
            row.update(link)
        return [str(row.get("设施编号")) for row in rows]

    def _sync_project(self, event: dict[str, Any], link: dict[str, Any]) -> list[str]:
        rows = [row for row in store.rows("project") if row.get("施工路段") == event["路段"]]
        for row in rows:
            row["工程状态"] = f"防汛{event['level']}联动管控"
            row["pending"] = True
            row["abnormal"] = _rank(event["level"]) >= _rank("橙色")
            row.update(link)
        return [str(row.get("工程编号")) for row in rows]

    # ------------------------------------------------------------------
    # 元数据：给前端表单用
    # ------------------------------------------------------------------
    def describe_sources(self) -> dict[str, Any]:
        return {
            "levels": LEVEL_ORDER,
            "command_types": COMMAND_TYPES,
            "sources": [
                {
                    "source_type": key,
                    "label": spec["label"],
                    "category": spec["category"],
                    "required": spec["required"],
                    "road_field": spec["road_field"],
                    "facility_field": spec["facility_field"],
                    "disposal_tree": [
                        {"key": node["key"], "title": node["title"], "requires": node["requires"]}
                        for node in DISPOSAL_TEMPLATES[key]
                    ],
                }
                for key, spec in SOURCE_TYPES.items()
            ],
        }
