from fupan import hwinfo

SAMPLE = {
    "cpu": [{"Name": "AMD Ryzen 7 7800X3D 8-Core Processor", "NumberOfCores": 8, "NumberOfLogicalProcessors": 16}],
    "gpu": [{"Name": "NVIDIA GeForce RTX 4070", "DriverVersion": "32.0.15.6094", "DriverDate": "2025-01-01",
             "AdapterRAM": 4293918720},
            {"Name": "AMD Radeon(TM) Graphics", "DriverVersion": "31", "DriverDate": "2024-01-01", "AdapterRAM": 536870912}],
    "ram": [{"Capacity": 17179869184, "ConfiguredClockSpeed": 2400, "SMBIOSMemoryType": 26}],
    "power": "电源方案 GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (平衡)",
    "battery": [{"BatteryStatus": 1}],
    "registry": {"game_mode": 0, "hags": 1, "background_capture": 1,
                 "vram": [{"name": "NVIDIA GeForce RTX 4070", "bytes": 12884901888}]},
    "displays": [{"index": 0, "device": "d1", "primary": True, "width": 2560, "height": 1440, "hz": 60, "max_hz": 165}],
}


def test_summarize_uses_registry_vram():
    pairs = hwinfo.summarize(SAMPLE)
    rows = dict(pairs)
    gpu_rows = [v for k, v in pairs if k == "显卡"]
    assert "RTX 4070（显存 12 GB" in gpu_rows[0]
    assert "16 GB DDR4，1 根" in rows["内存"]
    assert rows["电源计划"] == "平衡"


def test_analyze_flags_common_problems():
    items = {(f.level, f.item) for f in hwinfo.analyze(SAMPLE, recordings_free_gb=20)}
    assert ("warn", "显示器刷新率") in items
    assert ("info", "电源计划") in items
    assert ("warn", "游戏模式") in items
    assert ("warn", "后台录制") in items
    assert ("info", "内存通道") in items
    assert ("info", "内存频率") in items
    assert ("warn", "笔记本电源") in items
    assert ("info", "双显卡") in items
    assert ("warn", "录像硬盘空间") in items


def test_normalize_single_objects():
    raw = {"cpu": {"Name": "X"}, "battery": None, "power": "(高性能)"}
    out = hwinfo.normalize(raw)
    assert out["cpu"] == [{"Name": "X"}] and out["battery"] == [] and out["gpu"] == []
    assert out["power"] == "(高性能)"


def test_analyze_quiet_on_good_machine():
    good = {"displays": [{"index": 0, "hz": 165, "max_hz": 165}], "power": "(高性能)",
            "registry": {"game_mode": 1, "hags": 2, "background_capture": 0},
            "ram": [{"Capacity": 17179869184, "ConfiguredClockSpeed": 6000, "SMBIOSMemoryType": 34}] * 2,
            "battery": [{"BatteryStatus": 2}], "gpu": [{"Name": "NVIDIA GeForce RTX 4080"}]}
    assert all(f.level == "ok" for f in hwinfo.analyze(good, recordings_free_gb=500))


FULL = {
    "cpu": [{"Name": "AMD Ryzen 7 7700 8-Core Processor", "Manufacturer": "AuthenticAMD", "NumberOfCores": 8,
             "NumberOfLogicalProcessors": 16, "MaxClockSpeed": 3801, "L2CacheSize": 8192, "L3CacheSize": 32768,
             "SocketDesignation": "AM5"}],
    "gpu": [{"Name": "AMD Radeon(TM) Graphics", "DriverVersion": "32.0.21025.1024", "DriverDate": "2026-08-01",
             "AdapterRAM": 536870912},
            {"Name": "AMD Radeon RX 6750 GRE 12GB", "DriverVersion": "32.0.21025.1024", "DriverDate": "2026-08-01",
             "AdapterRAM": 4293918720}],
    "ram": [{"Capacity": 17179869184, "Speed": 4800, "ConfiguredClockSpeed": 4800, "Manufacturer": "Kingston",
             "PartNumber": "KF556C40-16      ", "DeviceLocator": "DIMM 1", "SMBIOSMemoryType": 34}],
    "memarray": [{"Use": 3, "MemoryDevices": 4, "MaxCapacityEx": 201326592}],
    "board": [{"Manufacturer": "ASUSTeK COMPUTER INC.", "Product": "TUF GAMING B650M-PLUS", "Version": "Rev 1.xx"}],
    "bios": [{"Manufacturer": "American Megatrends Inc.", "SMBIOSBIOSVersion": "3024", "ReleaseDate": "2025-05-10"}],
    "cs": [{"Manufacturer": "System manufacturer", "Model": "System Product Name", "PCSystemType": 1}],
    "os": [{"Caption": "Microsoft Windows 11 专业版", "Version": "10.0.26200", "OSArchitecture": "64 位"}],
    "disk": [{"FriendlyName": "Samsung SSD 990 PRO 1TB", "MediaType": "SSD", "BusType": "NVMe", "Size": 1000204886016}],
    "volume": [{"DriveLetter": "C", "SizeRemaining": 214748364800, "Size": 999653638144}],
    "monitor": [{"Name": "XV272U", "Maker": "ACR"}],
    "sound": [{"Name": "Realtek(R) Audio"}, {"Name": "AMD High Definition Audio Device"}, {"Name": "Realtek(R) Audio"}],
    "power": "电源方案 GUID: 381b4222  (平衡)",
    "registry": {"vram": [{"name": "AMD Radeon RX 6750 GRE 12GB", "bytes": 12884901888}]},
    "displays": [{"index": 0, "device": "d1", "primary": True, "width": 2560, "height": 1440, "hz": 180, "max_hz": 180}],
}


def test_spec_groups_full_config():
    groups = {g["group"]: {r["k"]: r["v"] for r in g["rows"]} for g in hwinfo.spec_groups(FULL)}
    assert list(groups) == ["处理器", "显卡", "内存", "主板", "硬盘", "显示器", "声音设备", "系统"]
    assert groups["处理器"] == {"型号": "AMD Ryzen 7 7700 8-Core Processor", "核心 / 线程": "8 核 16 线程",
                               "基础频率": "3.8 GHz", "缓存": "L2 8 MB · L3 32 MB", "插槽": "AM5"}
    assert groups["显卡"]["显卡 1"] == "AMD Radeon RX 6750 GRE 12GB（独立显卡，显存 12 GB）"  # 独显排第一
    assert groups["显卡"]["显卡 2"].startswith("AMD Radeon Graphics（核显")
    assert groups["内存"]["总容量"] == "16 GB DDR5 · 1 根 · 单通道"
    assert groups["内存"]["插槽"] == "已用 1 / 共 4 个（再插 1 根同型号内存就是双通道）"
    assert groups["内存"]["DIMM 1"] == "16 GB Kingston KF556C40-16 · 额定 4800 MT/s"
    assert groups["主板"]["型号"] == "ASUSTeK COMPUTER INC. TUF GAMING B650M-PLUS"
    assert groups["主板"]["BIOS"] == "American Megatrends Inc. 3024（2025-05-10）"
    assert groups["硬盘"]["Samsung SSD 990 PRO 1TB"] == "固态硬盘 · NVMe · 932 GB"
    assert groups["硬盘"]["C 盘"] == "可用 200 GB / 共 931 GB"
    assert groups["显示器"] == {"显示器 1（主屏）": "2560×1440 · 当前 180 Hz · 最高 180 Hz", "型号": "宏碁 XV272U"}
    assert groups["声音设备"] == {"设备 1": "Realtek Audio", "设备 2": "AMD High Definition Audio Device"}
    assert groups["系统"]["系统"] == "Microsoft Windows 11 专业版（版本 10.0.26200，64 位）"
    assert groups["系统"]["电脑类型"] == "台式机" and "品牌型号" not in groups["系统"]  # DIY 主板的占位文字不显示
    assert hwinfo.spec_summary(FULL) == "AMD Ryzen 7 7700 · Radeon RX 6750 GRE 12GB · 16GB DDR5 单通道 · 2560×1440 180Hz"


def test_spec_groups_empty_on_other_platforms():
    assert hwinfo.spec_groups({"platform": "darwin"}) == [] and hwinfo.spec_summary({}) == ""
