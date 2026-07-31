import httpx

TIMEOUT = 20


async def search(
    engine: str,
    query: str,
    *,
    searxng_url: str = "",
    keenable_api_key: str = "",
    max_results: int = 5,
) -> list[dict]:
    if engine == "searxng":
        return await _search_searxng(query, searxng_url, max_results)
    if engine == "keenable":
        return await _search_keenable(query, keenable_api_key, max_results)
    raise ValueError(f"Неизвестный поисковый движок: {engine!r}")


async def _search_searxng(query: str, base_url: str, max_results: int) -> list[dict]:
    if not base_url:
        raise RuntimeError("Не задан SEARXNG_URL")
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
        )
    return results


async def _search_keenable(query: str, api_key: str, max_results: int) -> list[dict]:
    # Keenable — keyless по умолчанию (без ключа работает с лимитом по IP),
    # ключ (X-API-Key) поднимает лимиты. https://docs.keenable.ai/
    headers = {"X-API-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.post(
            "https://api.keenable.ai/v1/search",
            headers=headers,
            json={"query": query},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet") or item.get("description", ""),
            }
        )
    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "(ничего не найдено)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} — {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)
