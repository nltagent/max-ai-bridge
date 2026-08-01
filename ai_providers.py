import logging
from typing import AsyncIterator

import httpx

from config import ProviderConfig

TIMEOUT = 60
logger = logging.getLogger(__name__)


async def ask(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None = None,
) -> str:
    """Получить полный ответ одной строкой (нестриминговый режим)."""
    chunks = []
    async for chunk in ask_stream(provider, model, system_prompt, history, prompt, extra_context):
        chunks.append(chunk)
    return "".join(chunks)


async def ask_stream(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None = None,
) -> AsyncIterator[str]:
    """Генератор чанков текста по мере поступления от провайдера."""
    if provider.kind == "openai_compatible":
        async for chunk in _stream_openai(provider, model, system_prompt, history, prompt, extra_context):
            yield chunk
    elif provider.kind == "gemini":
        async for chunk in _stream_gemini(provider, model, system_prompt, history, prompt, extra_context):
            yield chunk
    else:
        raise ValueError(f"Неизвестный тип провайдера: {provider.kind!r}")


def _build_user_content(prompt: str, extra_context: str | None) -> str:
    if not extra_context:
        return prompt
    return (
        f"{prompt}\n\n"
        f"--- Результаты веб-поиска (используй как контекст, если релевантно) ---\n{extra_context}"
    )


async def _stream_openai(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None,
) -> AsyncIterator[str]:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": _build_user_content(prompt, extra_context)})

    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        async with http.stream(
            "POST",
            f"{provider.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            json={"model": model, "messages": messages, "stream": True},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )

            buffer = ""
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    import json
                    obj = json.loads(data)
                except Exception:
                    continue

                if "error" in obj:
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Ошибка провайдера: {msg}")

                delta = obj.get("choices", [{}])[0].get("delta", {})
                text = delta.get("content") or ""
                if text:
                    yield text


async def _stream_gemini(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None,
) -> AsyncIterator[str]:
    def _role(r: str) -> str:
        return "model" if r == "assistant" else "user"

    contents = [{"role": _role(m["role"]), "parts": [{"text": m["content"]}]} for m in history]
    contents.append({"role": "user", "parts": [{"text": _build_user_content(prompt, extra_context)}]})

    body: dict = {"contents": contents}
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    url = f"{provider.base_url}/models/{model}:streamGenerateContent?alt=sse&key={provider.api_key}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        async with http.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            import json
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:])
                except Exception:
                    continue
                try:
                    yield obj["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError):
                    continue
