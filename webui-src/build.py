"""开发用：重新生成界面资源（只在开发机上运行，用户电脑不需要）。

1. 收集 app.js / index.html 用到的 Material Symbols 图标名，下载对应的图标字体子集
2. 用 Tailwind CLI 把 input.css 编译成 fupan/web/app.css（不再依赖在线 CDN）

用法：python3 webui-src/build.py [--icons]
"""

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "fupan" / "web"
EXTRA_ICONS = {"desktop_windows", "memory", "developer_board", "hourglass_top", "speed", "aspect_ratio",
               "monitor_heart", "expand_more", "bolt", "sports_esports", "videocam_off", "hard_drive",
               "memory_alt", "headphones", "settings"}  # hwinfo.spec_groups 里用的
NOT_ICONS = {"applied", "kept", "ok", "warn", "text"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def icon_names() -> list[str]:
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    names = set(re.findall(r'icon\("([a-z0-9_]+)"', js))
    names |= set(re.findall(r'material-symbols-outlined[^"]*">([a-z0-9_]+)<', html + js))
    for m in re.finditer(r'icon\(([^)]*?\?[^)]*?)\s*,', js):
        names |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    # 通过参数传给小部件的图标：aiCard("hearing", …)、profileTile("straighten", …)
    names |= set(re.findall(r'(?:aiCard|profileTile)\("([a-z0-9_]+)"', js))
    backend = (ROOT.parent / "fupan" / "optimizer.py")
    if backend.exists():
        names |= set(re.findall(r'"icon":\s*"([a-z_]+)"', backend.read_text(encoding="utf-8")))
    return sorted((names | EXTRA_ICONS) - NOT_ICONS)


def download_icons(names: list[str]) -> None:
    url = ("https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200"
           "&icon_names=" + ",".join(names) + "&display=block")
    css = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode()
    font_url = re.search(r"url\((https://[^)]+)\)", css).group(1)
    data = urllib.request.urlopen(urllib.request.Request(font_url, headers=UA), timeout=120).read()
    (WEB / "fonts" / "material-symbols.woff2").write_bytes(data)
    print(f"图标字体：{len(names)} 个图标，{len(data) // 1024} KB")


def build_css() -> None:
    cmd = ["npx", "--yes", "tailwindcss@3.4.17", "-c", str(ROOT / "tailwind.config.js"),
           "-i", str(ROOT / "input.css"), "-o", str(WEB / "app.css"), "--minify"]
    subprocess.run(cmd, check=True, cwd=ROOT)
    # 字体地址带上版本号，避免浏览器沿用旧缓存
    import hashlib
    stamp = hashlib.sha1(b"".join((WEB / "fonts" / f).read_bytes() for f in sorted(p.name for p in (WEB / "fonts").glob("*.woff2")))).hexdigest()[:8]
    css = (WEB / "app.css").read_text(encoding="utf-8").replace("__V__", stamp)
    (WEB / "app.css").write_text(css, encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    import re as _re
    html = _re.sub(r'href="app\.css(\?v=[0-9a-f]+)?"', f'href="app.css?v={stamp}"', html)
    (WEB / "index.html").write_text(html, encoding="utf-8")
    print("样式：", (WEB / "app.css").stat().st_size // 1024, "KB，版本", stamp)


if __name__ == "__main__":
    if "--icons" in sys.argv:
        download_icons(icon_names())
    build_css()
