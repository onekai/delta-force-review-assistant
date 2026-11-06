# 界面演示镜像：只包含网页界面和虚构的示例数据，不含录制和分析功能（那些只能在 Windows 上运行）。
# docker run --rm -p 8765:80 ghcr.io/onekai/delta-force-review-assistant
FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/onekai/delta-force-review-assistant"
LABEL org.opencontainers.image.description="三角洲复盘助手界面演示（示例数据）"
LABEL org.opencontainers.image.licenses="MIT"
COPY docker/demo.conf /etc/nginx/conf.d/default.conf
COPY fupan/web /usr/share/nginx/html
