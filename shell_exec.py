import asyncio
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 3500


@dataclass
class ShellResult:
    display_text: str   # то, что можно сразу отправить сообщением (может быть обрезано)
    full_output: str     # полный, необрезанный вывод — для вложения файлом
    truncated: bool       # True, если full_output длиннее display_text


async def run(cmd: str, timeout: int) -> ShellResult:
    """
    Выполняет shell-команду внутри контейнера, где работает бот,
    и возвращает её stdout+stderr — как обрезанный текст для чата,
    так и полный вывод (для отправки файлом, если он не влез).

    ВНИМАНИЕ: команда выполняется с теми же правами и доступом
    к файловой системе/сети, что и сам процесс бота — включая
    .env с ключами API. См. предупреждение в README.
    """
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        text = f"⏱️ Команда прервана по таймауту ({timeout}с)"
        return ShellResult(display_text=text, full_output=text, truncated=False)

    body = out.decode(errors="replace").strip() or "(пустой вывод)"
    footer = f"\n\n[exit code: {proc.returncode}]"
    full_output = f"$ {cmd}\n\n{body}{footer}"

    if len(full_output) <= MAX_OUTPUT_CHARS:
        return ShellResult(display_text=full_output, full_output=full_output, truncated=False)

    # оставляем немного места под футер с пометкой об обрезке
    truncated_body = body[: MAX_OUTPUT_CHARS - 200]
    display_text = f"$ {cmd}\n\n{truncated_body}\n… обрезано, полный вывод — файлом ниже{footer}"
    return ShellResult(display_text=display_text, full_output=full_output, truncated=True)
