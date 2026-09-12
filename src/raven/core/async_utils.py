"""Small cancellation primitives shared by Raven's durable runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar


_Params = ParamSpec("_Params")
_ResultT = TypeVar("_ResultT")


async def await_completion(
    future: asyncio.Future[_ResultT],
) -> tuple[_ResultT, bool]:
    """Wait for a started future and report cancellation only after it settles."""
    cancellation_requested = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancellation_requested = True

    return future.result(), cancellation_requested


async def run_in_thread(
    function: Callable[_Params, _ResultT],
    *args: _Params.args,
    **kwargs: _Params.kwargs,
) -> _ResultT:
    """Run synchronous work to completion before propagating cancellation."""
    future = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    result, cancellation_requested = await await_completion(future)
    if cancellation_requested:
        raise asyncio.CancelledError
    return result
