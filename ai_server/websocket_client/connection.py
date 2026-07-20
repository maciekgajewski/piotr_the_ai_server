from __future__ import annotations

import asyncio


class ConnectionInterrupted(Exception):
    pass


async def connect(session, url: str, heartbeat: float, stop_event: asyncio.Event):
    connect_task = asyncio.create_task(session.ws_connect(url, heartbeat=heartbeat))
    interrupted_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait((connect_task, interrupted_task), return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
    if interrupted_task in done:
        if connect_task.done() and not connect_task.cancelled():
            try:
                websocket = connect_task.result()
            except Exception:
                pass
            else:
                await websocket.close()
        raise ConnectionInterrupted()
    return connect_task.result()
