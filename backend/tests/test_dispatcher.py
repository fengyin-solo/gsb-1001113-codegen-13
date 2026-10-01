"""事件驱动分拨器测试：只依赖标准库，直接跑 `python3 -m unittest discover -s tests -v`。"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app.services.dispatcher import DispatcherService
from app.store import store


def _water_alarm(road: str, site: str, alarm_id: str, occurred_at: str,
                 current: str = "6.0", warning: str = "5.0", **extra):
    values = {
        "监测点": site, "影响路段": road, "当前水位": current, "警戒水位": warning,
        "告警编号": alarm_id, "发生时间": occurred_at,
    }
    values.update(extra)
    return "water_level", values


class DispatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._backup_tables = copy.deepcopy(store._tables)
        self._backup_versions = dict(store._versions)
        store._tables["dispatcher_event"] = []
        store._tables["dispatcher_command"] = []
        store._versions.clear()
        self.service = DispatcherService()

    def tearDown(self) -> None:
        store._tables = self._backup_tables
        store._versions = self._backup_versions

    # ------------------------------------------------------------------
    # 统一接入与处置树
    # ------------------------------------------------------------------
    def test_unified_intake_creates_event_with_topological_tree(self):
        event, message = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        self.assertIsNotNone(event, message)
        self.assertEqual(event["来源"], "水位告警")
        self.assertEqual(event["status"], "处置中")
        seen: set[str] = set()
        for node in event["disposal_tree"]:
            for req in node["requires"]:
                self.assertIn(req, seen, "前置件必须排在依赖它的节点之前")
            seen.add(node["key"])

    def test_all_four_sources_share_one_intake(self):
        cases = [
            ("water_level", {"监测点": "RK-01", "影响路段": "滨江路", "当前水位": "6.0", "警戒水位": "5.0"}),
            ("pump_offline", {"泵站编号": "PUMP-7", "影响路段": "滨江路", "离线时长": "5"}),
            ("slope_deviation", {"边坡编号": "SLOP-9", "所属路段": "环山北路", "位移量": "55"}),
            ("road_patrol", {"巡查编号": "PATR-9", "巡查路段": "中山大道", "发现问题": "路面积水"}),
        ]
        for source_type, values in cases:
            event, message = self.service.ingest_alarm(source_type, values)
            self.assertIsNotNone(event, f"{source_type}: {message}")
        self.assertEqual(len(store.rows("dispatcher_event")), 4)

    def test_missing_required_fields_are_reported(self):
        event, message = self.service.ingest_alarm("water_level", {"监测点": "RK-01"})
        self.assertIsNone(event)
        self.assertIn("缺少必填字段", message)

    # ------------------------------------------------------------------
    # 指纹合并与乱序重放
    # ------------------------------------------------------------------
    def test_duplicate_fingerprint_merges_into_one_event(self):
        first, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        second, message = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-2", "2026-10-01T08:05:00"))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["alarm_count"], 2)
        self.assertEqual(len(store.rows("dispatcher_event")), 1)
        self.assertIn("合并", message)

    def test_same_alarm_id_replay_is_idempotent(self):
        self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        event, message = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        self.assertEqual(event["alarm_count"], 1)
        self.assertEqual(event["version"], 1, "重复投递不产生新版本")
        self.assertIn("幂等", message)

    def test_out_of_order_replay_never_lowers_confirmed_level(self):
        event, _ = self.service.ingest_alarm(
            *_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00", current="6.5", warning="5.0")
        )
        self.assertEqual(event["level"], "橙色")
        self.assertEqual(event["confirmed_level"], "橙色")
        version_before = event["version"]
        # 迟到的低级别告警重放：时间更早、级别更低
        replayed, message = self.service.ingest_alarm(
            *_water_alarm("滨江路", "RK-01", "A-0", "2026-10-01T07:50:00", current="4.0", warning="5.0")
        )
        self.assertEqual(replayed["level"], "橙色", "乱序重放不能降低生效级别")
        self.assertEqual(replayed["confirmed_level"], "橙色", "乱序重放不能降低已确认级别")
        self.assertEqual(replayed["version"], version_before)
        self.assertTrue(replayed["alarms"][-1]["out_of_order"])
        self.assertIn("维持", message)

    # ------------------------------------------------------------------
    # 升级结论同步三方台账（版本 + 事务）
    # ------------------------------------------------------------------
    def _plant_ledgers(self, road: str) -> None:
        store.rows("flood").append({
            "id": 9001, "记录编号": "FLOO-9001", "影响路段": road, "预警级别": "蓝色",
            "status": "待响应", "pending": True, "abnormal": False,
        })
        store.rows("drainage").append({
            "id": 9002, "设施编号": "DRAI-9002", "所属路段": road, "淤积程度": "无",
            "设施状态": "正常", "status": "正常", "pending": False, "abnormal": False,
        })
        store.rows("project").append({
            "id": 9003, "工程编号": "PROJ-9003", "施工路段": road, "工程状态": "施工中",
            "status": "施工中", "pending": True, "abnormal": False,
        })

    def test_escalation_syncs_three_ledgers_in_one_version(self):
        road = "滨江路"
        self._plant_ledgers(road)
        event, _ = self.service.ingest_alarm(
            *_water_alarm(road, "RK-01", "A-1", "2026-10-01T08:00:00", current="6.5", warning="5.0")
        )
        flood = next(row for row in store.rows("flood") if row.get("影响路段") == road)
        drainage = next(row for row in store.rows("drainage") if row.get("所属路段") == road)
        project = next(row for row in store.rows("project") if row.get("施工路段") == road)
        self.assertEqual(flood["预警级别"], "橙色")
        self.assertEqual(flood["status"], "响应中")
        self.assertIn("联动", drainage["设施状态"])
        self.assertIn("联动", project["工程状态"])
        for row in (flood, drainage, project):
            self.assertEqual(row["联动事件"], event["事件编号"])
            self.assertEqual(row["联动版本"], event["version"], "三方台账必须挂同一个事件版本")
        self.assertEqual(len(event["sync_log"]), 1)
        self.assertTrue(event["sync_log"][0]["flood"])
        self.assertTrue(event["sync_log"][0]["drainage"])
        self.assertTrue(event["sync_log"][0]["project"])

    def test_failed_sync_rolls_back_all_modules(self):
        road = "滨江路"
        self._plant_ledgers(road)
        flood_before = copy.deepcopy(store.rows("flood"))
        with patch.object(DispatcherService, "_sync_project", side_effect=RuntimeError("模拟汇总页写入失败")):
            with self.assertRaises(RuntimeError):
                self.service.ingest_alarm(*_water_alarm(road, "RK-01", "A-1", "2026-10-01T08:00:00"))
        self.assertEqual(store.rows("flood"), flood_before, "事务回滚后防汛台账不能有半写状态")
        self.assertEqual(store.rows("dispatcher_event"), [], "事务回滚后事件不能残留")

    # ------------------------------------------------------------------
    # 指挥部命令与历史事件
    # ------------------------------------------------------------------
    def test_command_overrides_auto_escalation(self):
        event, _ = self.service.ingest_alarm(
            *_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00", current="6.5", warning="5.0")
        )
        self.assertEqual(event["level"], "橙色")
        command, message = self.service.issue_command(
            {"命令文号": "ZD-001", "路段": "滨江路", "类型": "现场管制", "管控级别": "黄色"}
        )
        self.assertIsNotNone(command, message)
        updated = self.service.get_event(event["id"])
        self.assertEqual(updated["level"], "黄色", "升级与管制冲突时以指挥部最新命令为准")
        self.assertEqual(updated["confirmed_level"], "橙色", "已确认高水位线不被命令拉低")
        self.assertEqual(updated["escalation_chain"][-1]["basis"], "指挥部命令ZD-001")

    def test_historical_event_keeps_its_escalation_chain(self):
        old, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        self.service.close_event(old["id"])
        chain_before = copy.deepcopy(old["escalation_chain"])
        active, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-02", "A-2", "2026-10-01T09:00:00"))
        self.service.issue_command({"命令文号": "ZD-002", "路段": "滨江路", "类型": "现场管制", "管控级别": "红色"})
        historical = self.service.get_event(old["id"])
        self.assertEqual(historical["escalation_chain"], chain_before, "历史事件维持此前升级链")
        self.assertEqual(historical["level"], old["level"])
        self.assertEqual(self.service.get_event(active["id"])["level"], "红色", "在办事件按最新命令裁决")

    def test_replayed_command_is_idempotent(self):
        self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        payload = {"命令文号": "ZD-003", "路段": "滨江路", "类型": "现场管制", "管控级别": "红色"}
        command, _ = self.service.issue_command(payload)
        replayed, message = self.service.issue_command(payload)
        self.assertIsNone(replayed)
        self.assertIn("幂等", message)
        self.assertEqual(len(store.rows("dispatcher_command")), 1)
        self.assertEqual(store.current_version("command_seq"), 1, "重放不能占用新的命令序号")

    def test_lift_control_restores_auto_level(self):
        event, _ = self.service.ingest_alarm(
            *_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00", current="6.5", warning="5.0")
        )
        self.service.issue_command({"命令文号": "ZD-004", "路段": "滨江路", "类型": "现场管制", "管控级别": "蓝色"})
        self.assertEqual(self.service.get_event(event["id"])["level"], "蓝色")
        self.service.issue_command({"命令文号": "ZD-005", "路段": "滨江路", "类型": "解除管制"})
        self.assertEqual(self.service.get_event(event["id"])["level"], "橙色", "解除管制后回到自动升级结论")

    # ------------------------------------------------------------------
    # 处置树前置件
    # ------------------------------------------------------------------
    def test_task_requires_prerequisites(self):
        event, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        blocked, message = self.service.complete_task(event["id"], "transfer")
        self.assertIsNone(blocked)
        self.assertIn("前置件未完成", message)
        self.service.complete_task(event["id"], "survey")
        self.service.complete_task(event["id"], "block")
        done, message = self.service.complete_task(event["id"], "transfer")
        self.assertIsNotNone(done, message)
        states = {node["key"]: node["state"] for node in done["disposal_tree"]}
        self.assertEqual(states["transfer"], "已完成")
        self.assertEqual(states["archive"], "待前置")

    def test_closed_event_stops_merging_and_tree(self):
        event, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-1", "2026-10-01T08:00:00"))
        self.service.close_event(event["id"])
        again, _ = self.service.ingest_alarm(*_water_alarm("滨江路", "RK-01", "A-2", "2026-10-01T09:00:00"))
        self.assertNotEqual(again["id"], event["id"], "事件结束后同指纹告警应生成新事件")
        blocked, message = self.service.complete_task(event["id"], "survey")
        self.assertIsNone(blocked)
        self.assertIn("已结束", message)


if __name__ == "__main__":
    unittest.main()
