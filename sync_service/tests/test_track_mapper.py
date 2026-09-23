"""track_mapper 纯函数单元测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import track_mapper
from app import config


def _day_ms(iso: str) -> int:
    """把 ISO 时间转成 UTC 当日 00:00:00 的毫秒时间戳。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp() * 1000)


class TestToUtcDayMs:
    def test_valid_iso_with_z(self):
        assert track_mapper._to_utc_day_ms("2026-09-15T05:46:17.000Z") == _day_ms(
            "2026-09-15T00:00:00+00:00"
        )

    def test_none_returns_none(self):
        assert track_mapper._to_utc_day_ms(None) is None

    def test_empty_string_returns_none(self):
        assert track_mapper._to_utc_day_ms("") is None

    def test_invalid_string_returns_none(self):
        assert track_mapper._to_utc_day_ms("not-a-date") is None


class TestMapTrackToNodes:
    def test_depart_and_arrive(self):
        traj = [
            {"code": "DA", "time": "2026-09-01T08:00:00Z", "content": "起飞"},
            {"code": "AA", "time": "2026-09-03T10:00:00Z", "content": "落地"},
        ]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_DEPART] == _day_ms("2026-09-01T00:00:00+00:00")
        assert result[config.F_ARRIVE] == _day_ms("2026-09-03T00:00:00+00:00")

    def test_sign_cc(self):
        traj = [{"code": "CC", "time": "2026-09-10T08:00:00Z", "content": "签收"}]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_SIGN] == _day_ms("2026-09-10T00:00:00+00:00")

    def test_cy11_requires_isa(self):
        # content 不含 ISA → 不算签收
        traj = [{"code": "CY11", "time": "2026-09-10T08:00:00Z", "content": "别的"}]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_SIGN] is None
        # content 含 ISA → 算签收
        traj2 = [{"code": "CY11", "time": "2026-09-10T08:00:00Z", "content": "ISA 签收"}]
        result2 = track_mapper.map_track_to_nodes(traj2)
        assert result2[config.F_SIGN] == _day_ms("2026-09-10T00:00:00+00:00")

    def test_special_rule_clearance_equals_delivery(self):
        # 有提柜(JL25)但无清关 → 清关时间 = 提柜时间
        traj = [{"code": "JL25", "time": "2026-09-05T08:00:00Z", "content": "提柜"}]
        result = track_mapper.map_track_to_nodes(traj)
        delivery = _day_ms("2026-09-05T00:00:00+00:00")
        assert result[config.F_DELIVERY] == delivery
        assert result[config.F_CLEARANCE] == delivery

    def test_inspection_texts(self):
        traj = [
            {"code": "CY", "time": "2026-09-02T08:00:00Z", "content": "被查验"},
            {"code": "CY05", "time": "2026-09-03T08:00:00Z", "content": "甩柜了"},
        ]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_INSPECTION] == "查验: 被查验; 甩柜: 甩柜了"

    def test_latest_track_from_is_latest(self):
        traj = [
            {"code": "XX", "time": "2026-09-02T08:00:00Z", "content": "普通"},
            {"code": "YY", "time": "2026-09-03T08:00:00Z", "content": "最新轨迹", "isLatest": True},
        ]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_LATEST_TRACK] == "最新轨迹"

    def test_empty_trajectory(self):
        result = track_mapper.map_track_to_nodes([])
        assert result[config.F_DEPART] is None
        assert result[config.F_SIGN] is None
        assert result[config.F_INSPECTION] == ""

    def test_code_case_insensitive(self):
        traj = [{"code": "da", "time": "2026-09-01T08:00:00Z", "content": "起飞"}]
        result = track_mapper.map_track_to_nodes(traj)
        assert result[config.F_DEPART] == _day_ms("2026-09-01T00:00:00+00:00")
