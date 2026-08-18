#!/usr/bin/env python3
"""多厂商模型交互层（零第三方依赖）。

支持两类协议：
  1. OpenAI 兼容协议（DeepSeek / 通义千问 DashScope / 智谱 GLM /
     Moonshot / Ollama 等）：POST {base_url}/chat/completions
  2. Anthropic Messages API：POST {base_url}/v1/messages

厂商配置放在 model_config.json，API Key 一律从环境变量读取（不落盘）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "model_config.json"
DEFAULT_TIMEOUT = 90


class ModelError(RuntimeError):
    """模型调用失败（配置缺失 / 网络 / 厂商返回错误）。"""


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        return {"default_provider": "", "providers": {}}
    try:
        with open(cfg_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"default_provider": "", "providers": {}}


def list_providers() -> list[dict]:
    """返回厂商列表（不含密钥），标注是否已配置 Key。"""
    cfg = load_config()
    default = cfg.get("default_provider", "")
    providers = []
    for name, spec in (cfg.get("providers") or {}).items():
        env_key = spec.get("api_key_env", "")
        key_present = bool(env_key and os.environ.get(env_key))
        # Ollama 本地服务不需要 Key
        if "127.0.0.1" in str(spec.get("base_url", "")) or "localhost" in str(spec.get("base_url", "")):
            key_present = True
        providers.append({
            "name": name,
            "type": spec.get("type", "openai"),
            "base_url": spec.get("base_url", ""),
            "model": spec.get("model", ""),
            "vision": bool(spec.get("vision", False)),
            "configured": key_present,
            "default": name == default,
        })
    return providers


def _provider(name: str) -> tuple[dict, dict]:
    cfg = load_config()
    providers = cfg.get("providers") or {}
    if not name:
        name = cfg.get("default_provider", "")
    if name not in providers:
        raise ModelError(
            f"未知模型厂商: {name or '(空)'}（可用: {', '.join(providers) or '无'}）"
        )
    spec = providers[name]
    env_key = spec.get("api_key_env", "")
    api_key = os.environ.get(env_key, "") if env_key else ""
    local = "127.0.0.1" in str(spec.get("base_url", "")) or "localhost" in str(spec.get("base_url", ""))
    if not api_key and not local:
        raise ModelError(
            f"厂商 {name} 未配置 API Key：请设置环境变量 {env_key}"
        )
    return spec, {"name": name, "api_key": api_key}


def _openai_chat(spec: dict, meta: dict, payload: dict) -> str:
    base_url = str(spec["base_url"]).rstrip("/")
    url = base_url + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {meta['api_key']}",
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ModelError(
            f"厂商 {meta['name']} HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:
        raise ModelError(f"厂商 {meta['name']} 请求失败: {exc}") from exc
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(
            f"厂商 {meta['name']} 返回格式异常: {json.dumps(result, ensure_ascii=False)[:300]}"
        ) from exc


def _anthropic_chat(spec: dict, meta: dict, payload: dict) -> str:
    base_url = str(spec["base_url"]).rstrip("/")
    url = base_url + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": meta["api_key"],
        "anthropic-version": spec.get("api_version", "2023-06-01"),
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ModelError(
            f"厂商 {meta['name']} HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:
        raise ModelError(f"厂商 {meta['name']} 请求失败: {exc}") from exc
    try:
        blocks = result.get("content") or []
        return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise ModelError(
            f"厂商 {meta['name']} 返回格式异常: {json.dumps(result, ensure_ascii=False)[:300]}"
        ) from exc


def _build_messages(system: str, user_text: str, images: list[dict] | None):
    """构造消息体。

    images: [{"mime": "image/jpeg", "data_b64": "..."}]
    有图时走多模态 content 结构（要求厂商 vision=true）。
    """
    if not images:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
    content: list[dict] = []
    for image in images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{image.get('mime', 'image/jpeg')};base64,{image['data_b64']}",
            },
        })
    content.append({"type": "text", "text": user_text})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def chat(
    provider: str | None,
    system: str,
    user_text: str,
    *,
    images: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """调用指定厂商模型，返回文本。"""
    spec, meta = _provider(provider)
    if images and not spec.get("vision"):
        raise ModelError(f"厂商 {meta['name']} 不支持图片输入（vision=false）")

    temperature = temperature if temperature is not None else float(spec.get("temperature", 0.1))
    max_tokens = max_tokens if max_tokens is not None else int(spec.get("max_tokens", 4000))
    ptype = spec.get("type", "openai")
    messages = _build_messages(system, user_text, images)

    if ptype == "anthropic":
        payload: dict = {
            "model": spec["model"],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m for m in messages if m["role"] != "system"],
        }
        system_text = "\n".join(
            m["content"] for m in messages if m["role"] == "system"
        )
        if system_text:
            payload["system"] = system_text
        return _anthropic_chat(spec, meta, payload)

    payload = {
        "model": spec["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return _openai_chat(spec, meta, payload)


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 ```json 围栏、前后说明与尾逗号）。"""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ModelError(f"模型输出中没有 JSON 对象: {text[:300]}")
    candidate = cleaned[start:end + 1]
    for repaired in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            continue
    raise ModelError(
        f"模型输出 JSON 解析失败: {candidate[:300]}"
    )


def chat_json(
    provider: str | None,
    system: str,
    user_text: str,
    *,
    images: list[dict] | None = None,
    max_tokens: int | None = None,
    retries: int = 1,
    log=None,
):
    """调用模型并解析 JSON；输出不合法时带修复提示重试（默认 1 次）。"""
    def _ask(text: str) -> str:
        return chat(
            provider,
            system,
            text,
            images=images,
            max_tokens=max_tokens,
        )

    text = _ask(user_text)
    for attempt in range(retries + 1):
        try:
            return extract_json(text), text
        except ModelError as exc:
            if attempt >= retries:
                raise
            if log is not None:
                try:
                    log(
                        f"MODEL JSON 解析失败，第 {attempt + 1} 次修复重试: {exc}",
                        "WARN",
                    )
                except Exception as retry_exc:
                    if log is not None:
                        log(
                            f"MODEL JSON 修复重试失败: {retry_exc}",
                            "WARN",
                        )
            text = _ask(
                f"{user_text}\n\n注意：你上一次输出的 JSON 不合法（{exc}）。"
                "请重新输出，必须是一个合法 JSON 对象，"
                "禁止任何说明文字、Markdown 代码围栏或尾逗号。"
            )
    raise ModelError("模型 JSON 解析失败")  # 不可达


def encode_image(path: str | Path, mime: str | None = None) -> dict:
    """读取本地图片为 {mime, data_b64}。"""
    image_path = Path(path)
    if not image_path.is_file():
        raise ModelError(f"图片不存在: {image_path}")
    suffix = image_path.suffix.lower()
    mime = mime or {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")
    data = image_path.read_bytes()
    return {"mime": mime, "data_b64": base64.b64encode(data).decode("ascii")}
