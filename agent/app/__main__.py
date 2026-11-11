"""Entry point: python -m app

Runs the panel on 127.0.0.1 (reached through `tailscale serve`, HTTPS) and,
optionally, directly on this machine's Tailscale IP (plain HTTP inside the
encrypted Tailscale tunnel). The direct listener is what users the machine is
*shared* with use, since Tailscale Serve isn't reachable for them.
"""
import asyncio
import contextlib
import subprocess

import uvicorn

from .config import CONFIG

COMMON = dict(log_level="warning", access_log=False, ws_ping_interval=20, ws_ping_timeout=20)


class _QuietServer(uvicorn.Server):
    """Secondary server: leaves signal handling to the main one."""
    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _tailscale_ipv4() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
        return ip if ip and ip.startswith("100.") else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


async def _direct(port: int, stop: asyncio.Event) -> None:
    """Bind on the Tailscale IP once tailscaled has one (it may come up after us)."""
    while not stop.is_set():
        ip = _tailscale_ipv4()
        if ip:
            # proxy_headers=False: never take the client address from X-Forwarded-For here
            cfg = uvicorn.Config("app.main:app", host=ip, port=port, lifespan="off", proxy_headers=False, **COMMON)
            srv = _QuietServer(cfg)
            task = asyncio.create_task(srv.serve())
            print(f"direct Tailscale listener on http://{ip}:{port}", flush=True)
            waiter = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                srv.should_exit = True
                await task
                return
            waiter.cancel()
            print("direct Tailscale listener stopped (IP changed?); retrying", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), 10)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    main_srv = uvicorn.Server(uvicorn.Config("app.main:app", host=CONFIG["server"]["host"],
                                             port=int(CONFIG["server"]["port"]), **COMMON))
    stop = asyncio.Event()
    port = CONFIG["server"].get("tailscale_direct_port")
    direct = asyncio.create_task(_direct(int(port), stop)) if port else None
    try:
        await main_srv.serve()
    finally:
        stop.set()
        if direct:
            await direct


if __name__ == "__main__":
    asyncio.run(main())
