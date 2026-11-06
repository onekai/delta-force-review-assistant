# 第三方组件

本项目的源代码以 MIT 许可发布。下面这些第三方组件各有自己的许可：

| 组件 | 用途 | 许可 | 是否包含在仓库里 |
|---|---|---|---|
| [PySide6 / Qt](https://www.qt.io/qt-for-python) | 桌面窗口、内置网页组件 | LGPL-3.0 | 否，安装时从 PyPI 下载 |
| [httpx](https://www.python-httpx.org/) | 网络请求 | BSD-3-Clause | 否 |
| [keyring](https://github.com/jaraco/keyring) | 把密钥存进 Windows 凭据管理器 | MIT | 否 |
| [pywinrt](https://github.com/pywinrt/pywinrt)（winrt-*） | 调用 Windows 自带文字识别 | MIT | 否 |
| [FFmpeg](https://ffmpeg.org/) | 录制、截帧、生成片段 | LGPL / GPL（取决于构建版本） | 否，安装脚本从官方发布页或镜像下载并校验 SHA256 |
| [PresentMon](https://github.com/GameTechDev/PresentMon) | 测量帧率 | MIT | 否，安装脚本下载并校验 SHA256 和英特尔数字签名 |
| [Tailwind CSS](https://tailwindcss.com/) | 生成界面样式（只在开发时使用） | MIT | 否，仓库里只有生成好的 `app.css` |
| [Inter](https://rsms.me/inter/) | 界面字体 | SIL Open Font License 1.1 | 是，`fupan/web/fonts/inter.woff2` |
| [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | 等宽字体 | SIL Open Font License 1.1 | 是，`fupan/web/fonts/jetbrains-mono.woff2` |
| [Material Symbols](https://fonts.google.com/icons) | 图标（只打包用到的子集） | Apache-2.0 | 是，`fupan/web/fonts/material-symbols.woff2` |

《三角洲行动》及相关名称、画面归腾讯所有，本项目与腾讯无关联。改枪码内容来自 B 站公开视频，版权归原作者，软件内标明作者和来源链接。
