"""AI 接口：国内几家大模型平台都提供 OpenAI 兼容格式，这里统一成一个客户端。

支持：DeepSeek、阿里云百炼（通义千问）、火山方舟（豆包）、智谱（GLM）。
只用三个接口：列出模型、文字对话、带图片的对话。所有错误都翻译成中文提示，密钥不会出现在报错或日志里。

两个坑（2026-10 实测 / 官方文档）：
1. 这些平台的新模型默认开着“思考模式”：先在内部推理再回答，推理也占输出额度。
   输出额度给得小时，正式回答会是空的。所以每次请求都按平台关掉思考（PROVIDERS[...]["extra"]）。
2. 给不能看图的模型发图片，接口有时照样返回 200，只是模型看不到图。所以必须用 vision_self_test 验证。
"""

from __future__ import annotations

import base64
import logging
import random
import struct
import zlib
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek", "base_url": "https://api.deepseek.com",
        "vision": ["deepseek-flash"], "text": ["deepseek-v4-pro", "deepseek-flash"],
        "text_only": ["deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "extra": {"thinking": {"type": "disabled"}},
        "console": "https://platform.deepseek.com/api_keys",
        "note": "能看图的是 deepseek-flash；deepseek-v4-pro 只能处理文字",
    },
    "qwen": {
        "label": "阿里云百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "vision": ["qwen3-vl-plus", "qwen3.7-plus", "qwen3.8-max"], "text": ["qwen3.7-plus", "qwen3.8-max"],
        "text_only": [], "extra": {"enable_thinking": False},
        "console": "https://bailian.console.aliyun.com/",
        "note": "看图推荐 qwen3-vl-plus；也可以把接口地址换成工作空间专属地址",
    },
    "doubao": {
        "label": "火山方舟（豆包）", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "vision": ["doubao-seed-2-1-pro-260915"], "text": ["doubao-seed-2-1-pro-260915"],
        "text_only": [], "extra": {"thinking": {"type": "disabled"}},
        "console": "https://console.volcengine.com/ark",
        "note": "模型名以方舟控制台“开通管理”里显示的为准",
    },
    "zhipu": {
        "label": "智谱（GLM）", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "vision": ["glm-4.5v"], "text": ["glm-4.5v"], "text_only": [],
        "extra": {"thinking": {"type": "disabled"}}, "raw_base64": True,
        "console": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "看图用 glm-4.5v",
    },
}
DEFAULT_PROVIDER = "deepseek"


class AIError(Exception):
    """给用户看的错误，message 已经是中文。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


_STATUS_MESSAGES = {
    400: "请求格式不被接受（可能是这个模型不支持图片）",
    401: "API Key 无效，请检查是否复制完整、是否选对了平台",
    402: "账户余额不足，请到平台控制台充值",
    403: "没有权限使用这个模型（可能还没在控制台开通）",
    404: "接口地址或模型名称不存在",
    422: "请求参数有误",
    429: "请求太频繁，请稍后再试",
    500: "AI 平台服务器出错，请稍后再试",
    503: "AI 平台服务繁忙，请稍后再试",
}


def _error_from_response(resp: httpx.Response) -> AIError:
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        detail = str((err or {}).get("message", "") if isinstance(err, dict) else (err or body.get("message", "")))[:200]
    except ValueError:
        pass
    message = _STATUS_MESSAGES.get(resp.status_code, f"请求失败（状态码 {resp.status_code}）")
    if detail:
        message += f"：{detail}"
    return AIError(message, resp.status_code)


@dataclass
class ChatResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning: str = ""


class AIClient:
    def __init__(self, api_key: str, base_url: str = "", provider: str = DEFAULT_PROVIDER, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise AIError("还没有填写 API Key")
        self.provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
        self.spec = PROVIDERS[self.provider]
        self._client = httpx.Client(
            base_url=(base_url or self.spec["base_url"]).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise AIError("连接超时，请检查网络") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"网络连接失败：{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise AIError("返回内容无法解析") from exc

    def list_models(self) -> list[str]:
        """有的平台没有模型列表接口（返回 404），这时返回空列表，由界面上的推荐模型补上。"""
        try:
            data = self._request("GET", "/models")
        except AIError as exc:
            if exc.status in (404, 405):
                return []
            raise
        return sorted(item.get("id", "") for item in data.get("data", []) if item.get("id"))

    def image_part(self, image_bytes: bytes, mime: str = "image/jpeg") -> dict:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        url = b64 if self.spec.get("raw_base64") else f"data:{mime};base64,{b64}"
        return {"type": "image_url", "image_url": {"url": url}}

    def chat(self, model: str, messages: list[dict], max_tokens: int = 800, temperature: float = 0.2) -> ChatResult:
        if not model:
            raise AIError("还没有选择模型")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                "stream": False, **self.spec.get("extra", {})}
        data = self._request("POST", "/chat/completions", json=body)
        choices = data.get("choices") or []
        if not choices:
            raise AIError("模型没有返回内容")
        usage = data.get("usage") or {}
        msg = choices[0].get("message") or {}
        return ChatResult(
            text=msg.get("content") or "",
            model=data.get("model", model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            reasoning=msg.get("reasoning_content") or "",
        )

    def ask_with_images(self, model: str, prompt: str, images: list[bytes], mime: str = "image/jpeg",
                        max_tokens: int = 800) -> ChatResult:
        content = [{"type": "text", "text": prompt}] + [self.image_part(img, mime) for img in images]
        return self.chat(model, [{"role": "user", "content": content}], max_tokens=max_tokens)

    def ask_with_image(self, model: str, prompt: str, image_bytes: bytes, mime: str = "image/jpeg",
                       max_tokens: int = 800) -> ChatResult:
        return self.ask_with_images(model, prompt, [image_bytes], mime, max_tokens)


# 兼容旧代码和测试里的名字
DeepSeekClient = AIClient


def pick_default_models(model_ids: list[str], provider: str = DEFAULT_PROVIDER) -> tuple[str, str]:
    """从可用模型里挑默认值：(文字模型, 看图模型)。列表为空时用这个平台推荐的模型。"""
    spec = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if not model_ids:
        return spec["text"][0], spec["vision"][0]
    lowered = {m.lower(): m for m in model_ids}

    def first(*needles: str, exclude: str = "") -> str:
        for needle in needles:
            for low, original in lowered.items():
                if needle in low and (not exclude or exclude not in low):
                    return original
        return ""

    vision = next((lowered[v.lower()] for v in spec["vision"] if v.lower() in lowered), "") or first("vision", "-vl", "vl-")
    text = next((lowered[t.lower()] for t in spec["text"] if t.lower() in lowered), "") or \
        first("v4-pro", "deepseek-flash", "flash", "deepseek-chat", exclude="vision") or \
        next((m for m in model_ids if m != vision), "")
    return text, vision


def solid_png(rgb: tuple[int, int, int], size: int = 96) -> bytes:
    """生成一张纯色 PNG（看图自检用，不需要额外的图片库）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SELF_TEST_COLORS = {"红": (220, 30, 30), "绿": (30, 170, 60), "蓝": (30, 70, 220), "黄": (240, 200, 20)}


def vision_self_test(client: AIClient, model: str) -> tuple[bool, str, ChatResult | None]:
    """发两张随机颜色的纯色图，模型要按顺序说对颜色才算真的能看图。"""
    names = random.sample(list(SELF_TEST_COLORS), 2)
    prompt = ("下面有两张图片，每张都是一种纯色。请按顺序说出两张图的颜色，只能从“红、绿、蓝、黄”里选，"
              "用逗号隔开，例如：红,蓝。不要输出别的内容。")
    try:
        result = client.ask_with_images(model, prompt, [solid_png(SELF_TEST_COLORS[n]) for n in names],
                                        mime="image/png", max_tokens=300)
    except AIError as exc:
        return False, f"{model} 不接受图片：{exc}", None
    seen = [ch for ch in result.text if ch in SELF_TEST_COLORS]
    if seen[:2] == names:
        return True, f"{model} 看图正常（两张测试图的颜色都说对了）", result
    if not result.text.strip() and result.reasoning:
        return False, f"{model} 只输出了思考过程、没有给出回答（可能这个模型不能关闭思考模式），请换一个看图模型", result
    hint = PROVIDERS.get(client.provider, {}).get("vision", [""])[0]
    return False, (f"{model} 看不到图片：测试图是“{'、'.join(names)}”，它回答“{result.text.strip()[:20]}”。"
                   + (f"请把看图模型换成 {hint}" if hint and hint != model else "请换一个能看图的模型或平台")), result


HUD_TEST_PROMPT = (
    "这是一张 PC 射击游戏的截图。请只根据画面回答，看不清就写“看不清”：\n"
    "1. 画面右下角的弹药数字是多少？\n"
    "2. 能否看到自己的血量条？大约剩多少？\n"
    "3. 画面里能看到敌人吗？大概在画面的什么位置？\n"
    "4. 有没有击杀提示、受击提示或死亡界面？\n"
    "用简短的编号列表回答。"
)
