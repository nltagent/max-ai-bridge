import httpx

from config import ProviderConfig

TIMEOUT = 60


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
    # подходит для OpenRouter, Groq и любого OpenAI-совместимого API/агрегатора
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
        return resp.json()["choices"][0]["message"]["content"]


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
    # нативный Google AI Studio API
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
        return data["candidates"][0]["content"]["parts"][0]["text"]
