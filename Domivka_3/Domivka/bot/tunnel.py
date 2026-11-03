"""Автоматичний публічний https-адрес для Mini App (Cloudflare Quick Tunnel).
Бот сам завантажує cloudflared (один раз), запускає тунель і повідомляє адресу, жодних ручних кроків."""
import asyncio
import logging
import platform
import re
import shutil
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "data" / "bin"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _asset():
    sysname, mach = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "386" if mach in ("i386", "i686", "x86") else "amd64"
    if sysname == "windows":
        return f"cloudflared-windows-{arch}.exe", "cloudflared.exe"
    if sysname == "darwin":
        return f"cloudflared-darwin-{'arm64' if arch == 'arm64' else 'amd64'}.tgz", "cloudflared"
    return f"cloudflared-linux-{arch}", "cloudflared"


def _download() -> Path:
    asset, name = _asset()
    dst = BIN / name
    BIN.mkdir(parents=True, exist_ok=True)
    tmp = BIN / (asset + ".part")
    urllib.request.urlretrieve(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}", tmp)
    if asset.endswith(".tgz"):
        with tarfile.open(tmp) as t:
            t.extract("cloudflared", BIN)
        tmp.unlink()
    else:
        tmp.replace(dst)
    dst.chmod(0o755)
    return dst


async def binary() -> Path:
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    dst = BIN / _asset()[1]
    if dst.exists():
        return dst
    logging.info("Завантажую cloudflared (один раз, ~40 МБ). Зачекайте…")
    return await asyncio.to_thread(_download)


def _ping(url: str) -> bool:
    try:
        return urllib.request.urlopen(url + "/api/ping", timeout=8).status == 200
    except Exception:
        return False


async def wait_reachable(url: str, tries: int = 25) -> bool:
    for _ in range(tries):
        if await asyncio.to_thread(_ping, url):
            return True
        await asyncio.sleep(3)
    return False


async def supervise(port: int, on_url):
    """Тримає тунель живим: якщо він падає, піднімає знову й повідомляє нову адресу через on_url(url)."""
    while True:
        try:
            exe = await binary()
            proc = await asyncio.create_subprocess_exec(str(exe), "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://localhost:{port}",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            found = None

            async def pump():
                nonlocal found
                async for raw in proc.stdout:
                    line = raw.decode(errors="ignore")
                    m = URL_RE.search(line)
                    if m and not found:
                        found = m.group(0)
                        asyncio.create_task(announce(found))
                    elif "ERR" in line and "error" in line.lower():
                        logging.debug("cloudflared: %s", line.strip())

            async def announce(url):
                if await wait_reachable(url):
                    await on_url(url)
                else:
                    logging.warning("Адреса %s створена, але поки не відповідає. Перевірте інтернет/антивірус або перезапустіть бота.", url)
                    await on_url(url)

            await pump()
            await proc.wait()
            logging.warning("Тунель зупинився. Перезапускаю за 5 секунд…")
        except Exception as e:
            logging.warning("Не вдалося запустити тунель: %s. Mini App недоступний, бот працює. Перезапуск за 30 с.", e)
            await asyncio.sleep(25)
        await asyncio.sleep(5)
