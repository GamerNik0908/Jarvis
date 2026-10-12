from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from jarvis.ui.terminal import cancel_on_sigint


@pytest.mark.skipif(
    sys.platform == "win32", reason="os.kill(SIGINT) verhält sich unter Windows anders"
)
async def test_ctrl_c_cancels_only_the_running_answer() -> None:
    previous = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(asyncio.sleep(10))
    with cancel_on_sigint(task):
        os.kill(os.getpid(), signal.SIGINT)
        with pytest.raises(asyncio.CancelledError):
            await task
    # Der aufrufende Task läuft weiter und der alte Handler ist wiederhergestellt.
    assert asyncio.current_task() is not None
    assert not asyncio.current_task().cancelling()  # type: ignore[union-attr]
    assert signal.getsignal(signal.SIGINT) is previous
