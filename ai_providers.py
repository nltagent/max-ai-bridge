import logging

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
    if provider.kind == "openai_compatible":
        return await _ask_openai_compatible(provider, model, system_prompt, history, prompt, extra_context)
    if provider.kind == "gemini":
        return await _ask_gemini(provider, model, system_prompt, history, prompt, extra_context)
    raise ValueError(f"Неизвестный тип провайдера: {provider.kind!r}")


def _build_user_content(prompt: str, extra_context: str | None) -> str:
    if not extra_context:
        return prompt
    return (
        f"{prompt}\n\n"
        f"--- Результаты веб-поиска (используй как контекст, если релевантно, "
        f"и не выдумывай источники сверх приведённых) ---\n{extra_context}"
    )


async def _ask_openai_compatible(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None,
) -> str:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": _build_user_content(prompt, extra_context)})

    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.post(
            f"{provider.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            json={"model": model, "messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()

    # некоторые провайдеры/модели возвращают 200 OK, но с полем "error"
    # вместо стандартного choices — логируем сырой ответ, чтобы было видно в логах
    if "error" in data:
        err = data["error"]
        logger.error(
            "Провайдер %s (%s) вернул ошибку в теле 200-ответа: %s",
            provider.name, model, err,
        )
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"Ошибка провайдера: {msg}")

    choices = data.get("choices")
    if not choices:
        logger.error(
            "Провайдер %s (%s) вернул пустой/отсутствующий choices. Полный ответ: %s",
            provider.name, model, data,
        )
        # если модель вернула finish_reason=content_filter или аналог — скажем об этом
        finish = (choices[0].get("finish_reason") if choices else None)
        if finish in ("content_filter", "stop", "length"):
            raise RuntimeError(f"Модель не вернула текст (finish_reason={finish!r})")
        raise RuntimeError("Провайдер вернул пустой ответ, подробности — в логах Railway")

    content = choices[0].get("message", {}).get("content")
    if not content:
        finish = choices[0].get("finish_reason")
        logger.error(
            "Провайдер %s (%s): content пустой, finish_reason=%r, choices[0]=%s",
            provider.name, model, finish, choices[0],
        )
        raise RuntimeError(f"Модель вернула пустой ответ (finish_reason={finish!r})")

    return content


def _role_to_gemini(role: str) -> str:
    return "model" if role == "assistant" else "user"


async def _ask_gemini(
    provider: ProviderConfig,
    model: str,
    system_prompt: str | None,
    history: list[dict],
    prompt: str,
    extra_context: str | None,
) -> str:
    contents = [
        {"role": _role_to_gemini(m["role"]), "parts": [{"text": m["content"]}]} for m in history
    ]
    contents.append({"role": "user", "parts": [{"text": _build_user_content(prompt, extra_context)}]})

    body: dict = {"contents": contents}
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    url = f"{provider.base_url}/models/{model}:generateContent?key={provider.api_key}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates")
    if not candidates:
        logger.error(
            "Gemini (%s): нет candidates в ответе. Полный ответ: %s",
            model, data,
        )
        raise RuntimeError("Gemini вернул пустой ответ, подробности — в логах Railway")

    return candidates[0]["content"]["parts"][0]["text"]
