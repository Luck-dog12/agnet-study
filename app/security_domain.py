"""Code-owned security domains that permission modes cannot relax.

The sealed-secret classifier is deliberately lexical: callers can apply it
before resolving, opening, scanning, or forwarding a path to a provider.  It
does not inspect file contents and therefore cannot accidentally classify a
normal source file merely because that file contains words such as ``secret``
or ``token``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypeAlias


PermissionModeName: TypeAlias = Literal["partial-control", "full-access"]

_SEALED_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".crt", ".cer", ".der", ".jks"}
)
_SEALED_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".docker",
        ".gnupg",
        ".kube",
        ".ssh",
        ".credentials",
        ".secrets",
        "credentials",
        "secrets",
    }
)
_SEALED_FILE_NAMES = frozenset(
    {
        ".dockerconfigjson",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "_netrc",
        "access-token",
        "access-token.json",
        "access_token",
        "access_token.json",
        "application_default_credentials.json",
        "auth.json",
        "client-secret.json",
        "client_secret.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "login-token",
        "login-token.json",
        "login.json",
        "login_token",
        "login_token.json",
        "oauth-token.json",
        "oauth_token.json",
        "refresh-token",
        "refresh-token.json",
        "refresh_token",
        "refresh_token.json",
        "service-account.json",
        "service_account.json",
        "token",
        "token.json",
        "tokens.json",
    }
)


class SealedSecretPathError(ValueError):
    """A path belongs to the non-bypassable sealed-secret domain."""


def _lexical_parts(path: object) -> tuple[str, ...] | None:
    if type(path) is not str or not path or "\x00" in path:
        return None
    # Treat both separators as separators on every host.  This makes a path
    # captured on Windows no less protected when evaluated on Linux, and vice
    # versa.  Empty/`.` components do not affect classification; path syntax
    # validation remains the responsibility of the caller.
    return tuple(
        part.casefold()
        for part in path.replace("\\", "/").split("/")
        if part not in {"", "."}
    )


def path_is_sealed_secret(path: object) -> bool:
    """Return whether *path* is a code-owned secret path, without filesystem I/O."""
    parts = _lexical_parts(path)
    if not parts:
        # Invalid representations are unsafe at this security boundary.  The
        # normal path validator will provide the public, sanitized error.
        return True
    # Every component is classified.  This intentionally rejects descendants
    # such as ``credentials/cache.json`` and even syntactically unusual paths
    # such as ``server.key/child`` without asking the filesystem whether the
    # component is currently a file or a directory.
    for part in parts:
        if (
            part in _SEALED_DIRECTORIES
            or part.startswith(".env")
            or part in _SEALED_FILE_NAMES
            or PurePosixPath(part).suffix.casefold() in _SEALED_SUFFIXES
        ):
            return True
    return False


def require_unsealed_secret_path(path: object) -> None:
    """Reject sealed paths with a fixed error that never reflects the input."""
    if path_is_sealed_secret(path):
        raise SealedSecretPathError("密封秘密路径拒绝访问。")


@dataclass(frozen=True, slots=True)
class AccountActionDecision:
    """Decision for future login, authorization, or credential actions."""

    authorized: bool
    requires_independent_trusted_approval: bool
    reason: str


def route_account_action_authorization(
    permission_mode: PermissionModeName,
    *,
    independent_trusted_approval: bool = False,
) -> AccountActionDecision:
    """Authorize an account action only from a separate trusted approval.

    ``permission_mode`` is validated but never grants authority.  Future
    account tools must pass only a host-controlled approval result here; the
    value must never come from model tool arguments.
    """
    if permission_mode not in {"partial-control", "full-access"}:
        raise ValueError("permission_mode 必须是 partial-control 或 full-access。")
    if type(independent_trusted_approval) is not bool:
        raise TypeError("independent_trusted_approval 必须是可信布尔结果。")
    if independent_trusted_approval:
        return AccountActionDecision(True, False, "independent trusted approval")
    return AccountActionDecision(False, True, "account action requires independent approval")


__all__ = [
    "AccountActionDecision",
    "SealedSecretPathError",
    "path_is_sealed_secret",
    "require_unsealed_secret_path",
    "route_account_action_authorization",
]
