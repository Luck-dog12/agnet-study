"""Explicit offline health-check doubles for CLI tests unrelated to networking."""

from unittest.mock import patch


class ReadyModelService:
    def __init__(self, settings, *, on_change):
        self.settings = settings
        self.on_change = on_change
        self.state = "checking"
        self.closed = False

    def start(self):
        self.set_state("ready")

    def set_state(self, state):
        self.state = state
        self.on_change(state)

    def mark_unavailable(self):
        self.set_state("unavailable")

    def close(self):
        self.closed = True


def ready_model_service_patch():
    """Network behavior is covered by test_model_failure_guard separately."""
    return patch("app.main.create_model_service_monitor", side_effect=ReadyModelService)
