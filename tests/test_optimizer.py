from fupan import optimizer, perfmon
from fupan.db import DB


class FakeSystem:
    """模拟 Windows 设置：注册表、电源计划、显示器。"""

    def __init__(self):
        self.reg = {(optimizer.GAMEBAR, "AutoGameModeEnabled"): 0,
                    (optimizer.GAMEDVR, "HistoricalCaptureEnabled"): 1}
        self.active = optimizer.BALANCED
        self.plans = {optimizer.BALANCED: "平衡", optimizer.HIGH_PERF: "高性能"}
        self.monitors = [{"index": 0, "device": r"\\.\DISPLAY1", "width": 2560, "height": 1440, "hz": 60, "max_hz": 165}]

    def reg_get(self, path, name):
        key = (path, name)
        return (True, self.reg[key]) if key in self.reg else (False, None)

    def reg_set(self, path, name, value):
        self.reg[(path, name)] = value

    def reg_delete(self, path, name):
        self.reg.pop((path, name), None)

    def power_active(self):
        return self.active, self.plans[self.active]

    def power_list(self):
        return dict(self.plans)

    def power_set(self, guid):
        if guid in self.plans:
            self.active = guid
            return True
        return False

    def power_duplicate(self, guid):
        self.plans["11111111-2222-3333-4444-555555555555"] = "高性能"
        return "11111111-2222-3333-4444-555555555555"

    def power_delete(self, guid):
        self.plans.pop(guid, None)

    def displays(self):
        return [dict(m) for m in self.monitors]

    def set_refresh(self, device, hz):
        self.monitors[0]["hz"] = hz
        return True, ""

    def top_processes(self):
        return [{"name": "chrome", "cpu": 18.0, "mem_mb": 900}]

    def file_exists(self, path):
        return True


def make(tmp_path):
    sysm = FakeSystem()
    return optimizer.Optimizer(DB(tmp_path / "t.db"), sysm), sysm


def test_apply_and_restore_everything(tmp_path):
    opt, sysm = make(tmp_path)
    before = dict(sysm.reg)
    msgs = opt.auto_persistent()
    assert len(msgs) == 3  # 刷新率、游戏模式、Xbox 录制
    assert sysm.monitors[0]["hz"] == 165
    assert sysm.reg[(optimizer.GAMEBAR, "AutoGameModeEnabled")] == 1
    assert sysm.reg[(optimizer.GAMEDVR, "HistoricalCaptureEnabled")] == 0
    assert opt.auto_persistent() == []  # 已经是最佳状态，不重复改
    assert "刷新率" in opt.restore()
    assert sysm.monitors[0]["hz"] == 60
    assert sysm.reg == before  # 原来没有的值被删掉，原来有的值改回原样
    assert opt.changed_items() == []


def test_session_power_plan_round_trip(tmp_path):
    opt, sysm = make(tmp_path)
    ok, msg = opt.apply_power(session=True)
    assert ok and sysm.active == optimizer.HIGH_PERF
    assert opt.restore_power(only_session=True) == "电源计划"
    assert sysm.active == optimizer.BALANCED


def test_power_plan_created_when_missing_and_removed_on_restore(tmp_path):
    opt, sysm = make(tmp_path)
    del sysm.plans[optimizer.HIGH_PERF]
    ok, _ = opt.apply_power(session=True)
    assert ok and sysm.active == "11111111-2222-3333-4444-555555555555"
    opt.restore_power()
    assert sysm.active == optimizer.BALANCED and "11111111-2222-3333-4444-555555555555" not in sysm.plans


def test_backup_keeps_first_original(tmp_path):
    opt, sysm = make(tmp_path)
    opt.apply_refresh()
    sysm.monitors[0]["hz"], sysm.monitors[0]["max_hz"] = 120, 165  # 用户自己改回了别的值
    opt.apply_refresh()
    opt.restore_refresh()
    assert sysm.monitors[0]["hz"] == 60


def test_items_reflect_state(tmp_path):
    opt, sysm = make(tmp_path)
    opt.sample_background()
    items = {it["key"]: it for it in opt.items({}, auto=False, exe_path="", free_gb=30)}
    assert items["refresh"]["status"] == "warn" and items["refresh"]["can_apply"]
    assert items["gamemode"]["status"] == "warn" and items["gamebar"]["status"] == "warn"
    assert items["background"]["status"] == "info" and "chrome" in items["background"]["detail"]
    assert items["storage"]["status"] == "warn"
    opt.auto_persistent()
    items = {it["key"]: it for it in opt.items({}, auto=True, exe_path="", free_gb=300)}
    assert items["refresh"]["status"] == "applied" and items["refresh"]["applied"]
    assert items["power"]["status"] == "ok"  # 开了自动优化：打游戏时才切高性能


def test_hybrid_detection():
    assert optimizer.is_hybrid(["intel(r) uhd graphics 770", "nvidia geforce rtx 4060 laptop gpu"])
    assert not optimizer.is_hybrid(["nvidia geforce rtx 4070"])


def test_presentmon_csv_both_versions(tmp_path):
    v2 = tmp_path / "v2.csv"
    v2.write_text("Application,ProcessID,FrameTime,CPUBusy\n" +
                  "".join(f"Game.exe,1,{6.0 if i % 50 else 20.0},1\n" for i in range(500)) +
                  "Other.exe,2,100,1\n", encoding="utf-8")
    frames = perfmon.parse_csv(v2, "game.exe")
    assert len(frames) == 500
    s = perfmon.summarize(frames)
    assert 150 < s["avg"] < 170 and s["low1"] == 50.0 and s["p99"] == 20.0 and len(s["series"]) <= 120
    assert max(s["series"]) == 20.0  # 卡顿尖峰没有被平均掉
    v1 = tmp_path / "v1.csv"
    v1.write_text("Application,MsBetweenPresents\n" + "Game.exe,10\n" * 40, encoding="utf-8")
    assert perfmon.summarize(perfmon.parse_csv(v1))["avg"] == 100.0
    assert perfmon.summarize([5.0] * 10) == {}
