import json

import httpx
import pytest

from fupan.aiclient import AIError, AIClient, pick_default_models


def make_client(handler):
    return AIClient("sk-test-1234567890", "https://api.example.com", transport=httpx.MockTransport(handler))


def test_list_models_and_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash-vision-exp"}]})

    with make_client(handler) as client:
        assert client.list_models() == ["deepseek-v4-flash-vision-exp", "deepseek-v4-pro"]
    assert seen == {"auth": "Bearer sk-test-1234567890", "path": "/models"}


@pytest.mark.parametrize("status,needle", [(401, "API Key 无效"), (402, "余额不足"), (429, "太频繁")])
def test_errors_are_translated(status, needle):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "x"}})

    with make_client(handler) as client, pytest.raises(AIError) as err:
        client.list_models()
    assert needle in str(err.value)
    assert "sk-test" not in str(err.value)


def test_image_request_shape():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "v", "choices": [{"message": {"content": "弹药 30"}}],
                                         "usage": {"prompt_tokens": 400, "completion_tokens": 20}})

    with make_client(handler) as client:
        result = client.ask_with_image("deepseek-v4-flash-vision-exp", "看图", b"\xff\xd8jpeg", "image/jpeg")
    part = captured["messages"][0]["content"][1]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result.text == "弹药 30" and result.prompt_tokens == 400


def test_missing_key_and_model():
    with pytest.raises(AIError):
        AIClient("")
    with make_client(lambda r: httpx.Response(200, json={})) as client, pytest.raises(AIError):
        client.chat("", [])


def test_network_error_is_friendly():
    def handler(request):
        raise httpx.ConnectError("boom")

    with make_client(handler) as client, pytest.raises(AIError) as err:
        client.list_models()
    assert "网络" in str(err.value)


def test_pick_default_models():
    assert pick_default_models(["deepseek-flash", "deepseek-v4-pro"]) == ("deepseek-v4-pro", "deepseek-flash")
    assert pick_default_models(["deepseek-chat", "deepseek-reasoner"]) == ("deepseek-chat", "")
    assert pick_default_models([], "qwen") == ("qwen3.7-plus", "qwen3-vl-plus")  # 没有模型列表接口时用推荐模型
    assert pick_default_models(["glm-4.5v"], "zhipu")[1] == "glm-4.5v"


@pytest.mark.parametrize("provider,key,value", [
    ("deepseek", "thinking", {"type": "disabled"}), ("doubao", "thinking", {"type": "disabled"}),
    ("zhipu", "thinking", {"type": "disabled"}), ("qwen", "enable_thinking", False)])
def test_thinking_is_disabled_for_every_provider(provider, key, value):
    """新模型默认先“思考”，思考也占输出额度；不关掉的话短回答会是空的（0.3.0 的看图自检就是这样失败的）。"""
    body = {}

    def handler(request):
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "红,蓝"}}], "usage": {}})

    client = AIClient("sk-test-1234567890", provider=provider, transport=httpx.MockTransport(handler))
    client.ask_with_image("m", "看图", b"\x89PNG", "image/png")
    assert body[key] == value
    url = body["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,") != (provider == "zhipu")  # 智谱要裸的 base64


def test_reasoning_only_answer_is_reported(monkeypatch):
    from fupan import aiclient
    monkeypatch.setattr(aiclient.random, "sample", lambda pop, k: ["红", "蓝"])

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "我先想想……"}}],
                                         "usage": {"prompt_tokens": 500, "completion_tokens": 300}})
    client = AIClient("sk-test-1234567890", transport=httpx.MockTransport(handler))
    ok, msg, _ = aiclient.vision_self_test(client, "deepseek-flash")
    assert not ok and "思考过程" in msg


def test_models_endpoint_missing_returns_empty():
    client = AIClient("sk-test-1234567890", provider="zhipu",
                      transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": {"message": "nf"}})))
    assert client.list_models() == []
