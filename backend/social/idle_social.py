"""Shared preemptible model call used by foreground social decisions."""
from __future__ import annotations

import asyncio
from contextlib import suppress


class SocialPreempted(Exception):
    pass


async def _generate(request, is_busy, reserve=None):
    task = asyncio.create_task(request)
    try:
        if reserve and not reserve():
            raise SocialPreempted()
        while not task.done():
            if is_busy():
                raise SocialPreempted()
            await asyncio.wait({task}, timeout=0.25)
        if is_busy():
            raise SocialPreempted()
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
