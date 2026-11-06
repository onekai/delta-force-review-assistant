import json
import time

from fupan import config, sessions
from fupan.logutil import redact


def test_config_defaults_when_missing(tmp_path):
    cfg = config.load(tmp_path / "none.json")
    assert cfg.auto_record is True
    assert cfg.retention_days == 3
    assert "三角洲行动" in cfg.game_title_keywords


def test_config_roundtrip_and_unknown_keys(tmp_path):
    path = tmp_path / "c.json"
    cfg = config.Config(retention_days=7, game_process_names=["Game.exe"])
    config.save(cfg, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["something_old"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = config.load(path)
    assert loaded.retention_days == 7
    assert loaded.game_process_names == ["Game.exe"]


def test_config_corrupt_file_falls_back(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    assert config.load(path).framerate == 60


def test_redact_hides_keys():
    text = "Authorization: Bearer sk-abcdef1234567890 api_key=sk-zzzzzzzzzzzz"
    out = redact(text)
    assert "abcdef1234567890" not in out
    assert "zzzzzzzzzzzz" not in out
    assert "***" in out


def test_sessions_cleanup_keeps_marked_and_active(tmp_path):
    now = time.time()
    old = tmp_path / "old"
    kept = tmp_path / "kept"
    active = tmp_path / "active"
    fresh = tmp_path / "fresh"
    for folder, started, keep in ((old, now - 5 * 86400, False), (kept, now - 9 * 86400, True),
                                  (active, now - 9 * 86400, False), (fresh, now - 3600, False)):
        sessions.write_session(folder, sessions.Session(name=folder.name, started_at=started, keep=keep))
    removed = sessions.cleanup(tmp_path, 3, now=now, active=active)
    assert removed == [old]
    assert kept.exists() and active.exists() and fresh.exists()


def test_session_duration_sums_runs(tmp_path):
    folder = tmp_path / "s"
    s = sessions.Session(name="s", started_at=100.0, runs=[sessions.Run(1, 100.0, 160.0), sessions.Run(2, 200.0, 230.0)])
    sessions.write_session(folder, s)
    (folder / "run01_000.mkv").write_bytes(b"x" * 10)
    summary = sessions.list_sessions(tmp_path)[0]
    assert summary.duration_sec == 90.0
    assert summary.video_files == 1
