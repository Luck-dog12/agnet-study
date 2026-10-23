from __future__ import annotations

import unittest

from app.event_queue import EventDispatcher
from app.schemas import (
    NormalHostEventData,
    TurnEndEventData,
    TurnStartEventData,
    UrgentHostEventData,
)
from app.workflow import SessionEventError, SessionEventWorkflow


class EventDispatcherTests(unittest.TestCase):
    def test_normal_events_batch_and_barrier_drains_in_order(self):
        workflow = SessionEventWorkflow.in_memory()
        dispatcher = EventDispatcher(workflow)
        first = dispatcher.submit_normal(
            "host/normal", NormalHostEventData(source="test", message="one")
        )
        second = dispatcher.submit_normal(
            "host/normal", NormalHostEventData(source="test", message="two")
        )
        self.assertFalse(first.done)
        barrier = dispatcher.submit_barrier(
            "host/normal", NormalHostEventData(source="test", message="barrier")
        )
        self.assertEqual(first.wait().seq, 1)
        self.assertEqual(second.wait().seq, 2)
        self.assertEqual(barrier.seq, 3)
        self.assertEqual([item.data.message for item in workflow.events], ["one", "two", "barrier"])
        dispatcher.close()

    def test_urgent_cancels_generation_and_is_committed_last(self):
        workflow = SessionEventWorkflow.in_memory()
        calls: list[str] = []
        dispatcher = EventDispatcher(workflow, cancel_generation=lambda: calls.append("cancel"))
        receipt = dispatcher.submit_normal(
            "host/normal", NormalHostEventData(source="test", message="queued")
        )
        urgent = dispatcher.submit_urgent(
            "host/urgent", UrgentHostEventData(source="user", message="stop")
        )
        self.assertEqual(calls, ["cancel"])
        self.assertEqual(receipt.wait().seq, 1)
        self.assertEqual(urgent.seq, 2)
        self.assertEqual([item.type for item in workflow.events], ["host/normal", "host/urgent"])
        dispatcher.close()

    def test_batch_state_preflight_assigns_no_partial_sequence(self):
        workflow = SessionEventWorkflow.in_memory()
        with self.assertRaises(SessionEventError):
            workflow.append_batch((
                ("host/normal", NormalHostEventData(source="test", message="valid")),
                ("turn/end", TurnEndEventData(turn_id="a" * 32, kind="interrupted")),
            ))
        self.assertEqual(workflow.events, ())

    def test_barrier_batch_preflights_complete_unit_after_draining_normal_events(self):
        workflow = SessionEventWorkflow.in_memory()
        dispatcher = EventDispatcher(workflow)
        receipt = dispatcher.submit_normal(
            "host/normal", NormalHostEventData(source="test", message="before")
        )
        turn_id = "a" * 32
        committed = dispatcher.submit_barrier_batch((
            ("turn/start", TurnStartEventData(turn_id=turn_id)),
            ("turn/end", TurnEndEventData(turn_id=turn_id, kind="interrupted")),
        ))
        self.assertEqual(receipt.wait().seq, 1)
        self.assertEqual([item.seq for item in committed], [2, 3])
        self.assertEqual([item.type for item in workflow.events], ["host/normal", "turn/start", "turn/end"])
        dispatcher.close()


if __name__ == "__main__":
    unittest.main()
