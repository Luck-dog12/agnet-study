"""Private worker for the explicitly gated cross-process stale Sandbox E2E.

The worker deliberately has no diagnostic output surface.  It emits one
fixed token on success (or one fixed failure token) so provider data,
credentials, Sandbox identifiers, and OSS paths cannot cross the child
process boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Final


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.aliyun_sandbox_probe import _create_sandbox, build_oss_metadata
from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxSettings, load_sandbox_settings


LIVE_GATE: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_CROSS_PROCESS_STALE_E2E"
STAGE_ENV: Final[str] = "AGNET_LIVE_STALE_STAGE"
SESSION_ENV: Final[str] = "AGNET_LIVE_STALE_SESSION"
CREATE_STAGE: Final[str] = "create-stale"
CLEANUP_STAGE: Final[str] = "cleanup"
CREATE_OK_TOKEN: Final[str] = "AGNET_STALE_CREATED"
CLEANUP_OK_TOKEN: Final[str] = "AGNET_STALE_CLEANED"
FAILURE_TOKEN: Final[str] = "AGNET_STALE_WORKER_FAILED"

_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")


def _validated_request() -> tuple[str, str]:
    if os.environ.get(LIVE_GATE) != "1":
        raise RuntimeError("live gate disabled")
    stage = os.environ.get(STAGE_ENV, "")
    if stage not in {CREATE_STAGE, CLEANUP_STAGE}:
        raise RuntimeError("invalid stage")
    session_id = os.environ.get(SESSION_ENV, "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise RuntimeError("invalid session")
    return stage, session_id


def _settings() -> SandboxSettings:
    settings = load_sandbox_settings()
    settings.require_enabled()
    settings.require_recovery_enabled()
    return settings


def _best_effort_cleanup(settings: SandboxSettings, session_id: str) -> None:
    try:
        cleanup_session_sandboxes(settings, session_id)
    except BaseException:
        # The public worker boundary must never echo third-party exception data.
        pass


def _create_stale(
    settings: SandboxSettings,
    session_id: str,
) -> None:
    metadata = build_oss_metadata(settings)
    metadata[SANDBOX_OWNER_METADATA_KEY] = SANDBOX_OWNER_METADATA_VALUE
    metadata[SANDBOX_SESSION_METADATA_KEY] = session_id
    create_attempted = False
    intentional_handoff = False
    try:
        create_attempted = True
        sandbox = _create_sandbox(settings, metadata)
        sandbox_id = getattr(sandbox, "sandbox_id", None)
        if (
            not isinstance(sandbox_id, str)
            or not sandbox_id
            or len(sandbox_id) > 256
            or any(
                ord(character) < 32 or character.isspace()
                for character in sandbox_id
            )
        ):
            raise RuntimeError("invalid create result")
        intentional_handoff = True
    finally:
        # Success intentionally leaves exactly this random session's Sandbox
        # active for process B.  Any ambiguous or failed create is cleaned by
        # the independent session-scoped product cleaner instead.
        if create_attempted and not intentional_handoff:
            _best_effort_cleanup(settings, session_id)


def _run() -> str:
    stage, session_id = _validated_request()
    settings = _settings()
    if stage == CREATE_STAGE:
        _create_stale(settings, session_id)
        return CREATE_OK_TOKEN

    cleanup_succeeded = False
    try:
        cleanup_session_sandboxes(settings, session_id)
        cleanup_succeeded = True
        return CLEANUP_OK_TOKEN
    finally:
        if not cleanup_succeeded:
            _best_effort_cleanup(settings, session_id)


def main() -> int:
    try:
        token = _run()
    except BaseException:
        print(FAILURE_TOKEN, flush=True)
        return 1
    print(token, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
