import asyncio

from app.agent.run_fence import side_effects_allowed
from app.agent.run_manager import RunManager


class _FenceDb:
    def __init__(self, row=None):
        self.row = row
        self.causes = []
        self.interrupts = []

    async def run_state_get(self, _session_id):
        return self.row

    async def run_state_set_cause(self, _session_id, cause):
        self.causes.append(cause)
        if self.row is not None:
            self.row["stop_cause"] = cause

    async def set_interrupt(self, session_id):
        self.interrupts.append(session_id)


def test_durable_fence_rejects_replaced_generation():
    db = _FenceDb({"turn_id": "turn-2", "stop_cause": None})
    assert not asyncio.run(side_effects_allowed(
        db, "session", expected_turn_id="turn-1"))


def test_durable_fence_rejects_user_stop():
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "user_stop"})
    assert not asyncio.run(side_effects_allowed(
        db, "session", expected_turn_id="turn-1"))


def test_stop_button_cancels_auxiliary_without_poisoning_next_turn():
    asyncio.run(_assert_stop_button_cancels_auxiliary())


async def _assert_stop_button_cancels_auxiliary():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "complete"})
    started = asyncio.Event()

    async def one_shot():
        manager.register_auxiliary("session", turn_id="turn-1")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(one_shot())
    await started.wait()
    assert manager.has_auxiliary("session")

    assert await manager.interrupt("session", db, cause="user_stop")
    await asyncio.sleep(0)

    assert task.cancelled()
    assert not manager.has_auxiliary("session")
    assert db.causes == ["user_stop"]
    # There was no main agent loop to consume an interrupt flag.
    assert db.interrupts == []


def test_app_cancel_all_cancels_auxiliary_only_session():
    asyncio.run(_assert_app_cancel_all_cancels_auxiliary())


async def _assert_app_cancel_all_cancels_auxiliary():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "complete"})
    db.run_state_tag_resumable_as_user_stop = _return_zero
    started = asyncio.Event()

    async def one_shot():
        manager.register_auxiliary("session", turn_id="turn-1")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(one_shot())
    await started.wait()

    assert await manager.cancel_all(db) == 0
    await asyncio.sleep(0)
    assert task.cancelled()
    assert db.causes == ["user_stop"]


async def _return_zero():
    return 0
