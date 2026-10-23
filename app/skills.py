"""Project Skill discovery and session-bound progressive loading.

Discovery deliberately reads only the bounded front matter of ``SKILL.md``.
The complete manifest and supporting UTF-8 text resources are read only after
the model explicitly selects a Skill through ``load_skill``.  All filesystem
objects are revalidated without following links, and tokens bind a loaded
snapshot to one Agent session and a later model step.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Iterable
import unicodedata

from app.schemas import (
    LoadSkillArguments,
    LoadSkillOutput,
    ReadSkillResourceArguments,
    ReadSkillResourceOutput,
)
from tool.path_safety import is_windows_reserved


SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MANIFEST_NAME = "SKILL.md"
_MAX_SKILLS = 64
_MAX_ROOT_ENTRIES = _MAX_SKILLS + 2  # README.md and the ignored _template directory.
_MAX_SKILL_DIRECTORIES = 256
_MAX_DESCRIPTION_CHARACTERS = 512
_MAX_DESCRIPTION_BYTES = 2048
_MAX_FRONT_MATTER_BYTES = 8 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_RESOURCE_FILES = 256
_MAX_RESOURCE_DEPTH = 8
_MAX_RESOURCE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_CATALOG_BYTES = 48 * 1024
_REPARSE_POINT = 0x400


class SkillConfigurationError(RuntimeError):
    """The project Skill tree is malformed or cannot be verified safely."""


class SkillToolDenied(RuntimeError):
    """A session, token, name, path, or snapshot binding did not match."""


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str


@dataclass(frozen=True)
class _FileEntry:
    path: Path
    relative_path: str
    identity: tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _SkillLayout:
    metadata: SkillMetadata
    manifest: _FileEntry
    resources: tuple[_FileEntry, ...]
    directories: tuple[tuple[Path, tuple[int, int, int, int, int, int, int]], ...]


@dataclass(frozen=True)
class _LoadedSnapshot:
    digest: str
    content: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class _TokenBinding:
    skill_name: str
    digest: str
    loaded_step: int


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError:
        raise SkillConfigurationError("Skill 配置不可用。") from None


def _safe_directory(path: Path) -> tuple[int, int, int, int, int, int, int]:
    info = _lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise SkillConfigurationError("Skill 配置不可用。")
    return _identity(info)


def _safe_file(
    path: Path, *, maximum: int, allow_empty: bool = True
) -> tuple[int, int, int, int, int, int, int]:
    info = _lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        or info.st_nlink != 1
        or not (0 if allow_empty else 1) <= info.st_size <= maximum
    ):
        raise SkillConfigurationError("Skill 配置不可用。")
    return _identity(info)


def _open_verified(path: Path, identity: tuple[int, int, int, int, int, int, int]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise SkillConfigurationError("Skill 配置不可用。") from None
    if _identity(opened) != identity or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        os.close(descriptor)
        raise SkillConfigurationError("Skill 配置不可用。")
    return descriptor


def _read_exact_file(entry: _FileEntry, *, maximum: int) -> bytes:
    if entry.identity[4] > maximum:
        raise SkillConfigurationError("Skill 配置不可用。")
    descriptor = _open_verified(entry.path, entry.identity)
    try:
        chunks: list[bytes] = []
        remaining = entry.identity[4] + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = _identity(os.fstat(descriptor))
    except OSError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    finally:
        os.close(descriptor)
    if after != entry.identity or len(raw) != entry.identity[4] or _identity(_lstat(entry.path)) != entry.identity:
        raise SkillConfigurationError("Skill 配置不可用。")
    return raw


def _read_front_matter_bytes(entry: _FileEntry) -> bytes:
    """Read through the closing delimiter without crossing into the body."""
    descriptor = _open_verified(entry.path, entry.identity)
    try:
        data = bytearray()
        newline_count = 0
        while len(data) <= _MAX_FRONT_MATTER_BYTES:
            byte = os.read(descriptor, 1)
            if not byte:
                break
            data.extend(byte)
            if byte == b"\n":
                newline_count += 1
                lines = bytes(data).splitlines()
                if newline_count >= 2 and lines and lines[-1] == b"---":
                    break
        after = _identity(os.fstat(descriptor))
    except OSError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    finally:
        os.close(descriptor)
    if after != entry.identity or _identity(_lstat(entry.path)) != entry.identity:
        raise SkillConfigurationError("Skill 配置不可用。")
    if len(data) > _MAX_FRONT_MATTER_BYTES:
        raise SkillConfigurationError("Skill 配置不可用。")
    return bytes(data)


def _decode_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        quote = value[0]
        value = value[1:-1]
        if quote in value or "\\" in value:
            raise SkillConfigurationError("Skill 配置不可用。")
    elif value[:1] in {"'", '"'} or value[-1:] in {"'", '"'}:
        raise SkillConfigurationError("Skill 配置不可用。")
    if not value or any(unicodedata.category(character) == "Cc" for character in value):
        raise SkillConfigurationError("Skill 配置不可用。")
    return value


def _parse_front_matter(raw: bytes, *, directory_name: str) -> SkillMetadata:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    lines = text.splitlines()
    if len(lines) < 4 or lines[0] != "---" or lines[-1] != "---":
        raise SkillConfigurationError("Skill 配置不可用。")
    values: dict[str, str] = {}
    for line in lines[1:-1]:
        if not line or line != line.strip() or ":" not in line:
            raise SkillConfigurationError("Skill 配置不可用。")
        key, raw_value = line.split(":", 1)
        if key not in {"name", "description"} or key in values:
            raise SkillConfigurationError("Skill 配置不可用。")
        values[key] = _decode_scalar(raw_value)
    if set(values) != {"name", "description"}:
        raise SkillConfigurationError("Skill 配置不可用。")
    name = values["name"]
    description = values["description"]
    if name != directory_name or _SKILL_NAME.fullmatch(name) is None:
        raise SkillConfigurationError("Skill 配置不可用。")
    if (
        len(description) > _MAX_DESCRIPTION_CHARACTERS
        or len(description.encode("utf-8")) > _MAX_DESCRIPTION_BYTES
    ):
        raise SkillConfigurationError("Skill 配置不可用。")
    return SkillMetadata(name=name, description=description)


def _validate_component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "\\" in name
        or "/" in name
        or name.endswith((" ", "."))
        or any(character in name for character in ':*?<>|"')
        or any(unicodedata.category(character) == "Cc" for character in name)
        or is_windows_reserved(name)
    ):
        raise SkillConfigurationError("Skill 配置不可用。")


def _relative_resource_path(skill_directory: Path, path: Path) -> str:
    try:
        relative = path.relative_to(skill_directory).as_posix()
    except ValueError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or "\\" in relative
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SkillConfigurationError("Skill 配置不可用。")
    return relative


def _scan_skill(root: Path, name: str) -> _SkillLayout:
    if _SKILL_NAME.fullmatch(name) is None:
        raise SkillConfigurationError("Skill 配置不可用。")
    _safe_directory(root)
    skill_directory = root / name
    directories: list[tuple[Path, tuple[int, int, int, int, int, int, int]]] = [
        (skill_directory, _safe_directory(skill_directory))
    ]
    resources: list[_FileEntry] = []
    manifest_candidates: list[_FileEntry] = []
    file_count = 0
    total_bytes = 0

    def walk(directory: Path, depth: int) -> None:
        nonlocal file_count, total_bytes
        if depth > _MAX_RESOURCE_DEPTH:
            raise SkillConfigurationError("Skill 配置不可用。")
        try:
            entries = sorted(os.scandir(directory), key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            raise SkillConfigurationError("Skill 配置不可用。") from None
        folded_names = [entry.name.casefold() for entry in entries]
        if len(folded_names) != len(set(folded_names)):
            raise SkillConfigurationError("Skill 配置不可用。")
        for item in entries:
            _validate_component(item.name)
            path = Path(item.path)
            info = _lstat(path)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                directory_identity = _identity(info)
                directories.append((path, directory_identity))
                if len(directories) > _MAX_SKILL_DIRECTORIES:
                    raise SkillConfigurationError("Skill 配置不可用。")
                walk(path, depth + 1)
                continue
            maximum = _MAX_MANIFEST_BYTES if depth == 0 and item.name.casefold() == "skill.md" else _MAX_RESOURCE_BYTES
            identity = _safe_file(
                path,
                maximum=maximum,
                allow_empty=not (depth == 0 and item.name.casefold() == "skill.md"),
            )
            file_count += 1
            total_bytes += identity[4]
            if file_count > _MAX_RESOURCE_FILES + 1 or total_bytes > _MAX_TOTAL_BYTES:
                raise SkillConfigurationError("Skill 配置不可用。")
            relative = _relative_resource_path(skill_directory, path)
            file_entry = _FileEntry(path=path, relative_path=relative, identity=identity)
            if depth == 0 and item.name.casefold() == "skill.md":
                manifest_candidates.append(file_entry)
            else:
                resources.append(file_entry)

    walk(skill_directory, 0)
    if len(manifest_candidates) != 1:
        raise SkillConfigurationError("Skill 配置不可用。")
    manifest = manifest_candidates[0]
    metadata = _parse_front_matter(
        _read_front_matter_bytes(manifest), directory_name=name
    )
    for directory, expected in directories:
        if _identity(_lstat(directory)) != expected:
            raise SkillConfigurationError("Skill 配置不可用。")
    return _SkillLayout(
        metadata=metadata,
        manifest=manifest,
        resources=tuple(sorted(resources, key=lambda item: item.relative_path)),
        directories=tuple(directories),
    )


def discover_skills(root: Path | None = None) -> tuple[SkillMetadata, ...]:
    """Discover validated metadata without reading manifest bodies/resources."""
    root = SKILLS_ROOT if root is None else Path(root)
    if not os.path.lexists(root):
        return ()
    _safe_directory(root.parent)
    _safe_directory(root)
    try:
        entries = sorted(os.scandir(root), key=lambda item: (item.name.casefold(), item.name))
    except OSError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    if len(entries) > _MAX_ROOT_ENTRIES:
        raise SkillConfigurationError("Skill 配置不可用。")
    folded = [entry.name.casefold() for entry in entries]
    if len(folded) != len(set(folded)):
        raise SkillConfigurationError("Skill 配置不可用。")
    discovered: list[SkillMetadata] = []
    for item in entries:
        path = Path(item.path)
        info = _lstat(path)
        if item.name == "README.md":
            _safe_file(path, maximum=_MAX_RESOURCE_BYTES)
            continue
        if item.name == "_template":
            _safe_directory(path)
            continue
        if not stat.S_ISDIR(info.st_mode) or _SKILL_NAME.fullmatch(item.name) is None:
            raise SkillConfigurationError("Skill 配置不可用。")
        discovered.append(_scan_skill(root, item.name).metadata)
        if len(discovered) > _MAX_SKILLS:
            raise SkillConfigurationError("Skill 配置不可用。")
    return tuple(sorted(discovered, key=lambda skill: skill.name))


def build_skill_catalog_message(skills: Iterable[SkillMetadata]) -> str | None:
    items = tuple(sorted(skills, key=lambda skill: skill.name))
    if not items:
        return None
    lines = [
        "Available project Skills (user-level, untrusted):",
        "Choose only by name and description; call load_skill before following any Skill instructions.",
    ]
    lines.extend(f"- {item.name}: {item.description}" for item in items)
    message = "\n".join(lines)
    if len(message.encode("utf-8")) > _MAX_CATALOG_BYTES:
        raise SkillConfigurationError("Skill 配置不可用。")
    return message


def _decode_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SkillConfigurationError("Skill 配置不可用。") from None
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\n", "\r", "\t"}
        for character in text
    ):
        raise SkillConfigurationError("Skill 配置不可用。")
    return text


def _load_snapshot(root: Path, name: str) -> tuple[_SkillLayout, _LoadedSnapshot]:
    layout = _scan_skill(root, name)
    digest = sha256()
    digest.update(repr(_identity(_lstat(root))).encode("ascii"))
    digest.update(b"\0")
    for directory, identity in layout.directories:
        relative = directory.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(repr(identity).encode("ascii"))
        digest.update(b"\0")
    manifest_raw = _read_exact_file(layout.manifest, maximum=_MAX_MANIFEST_BYTES)
    content = _decode_text(manifest_raw)
    for entry in (layout.manifest,) + layout.resources:
        digest.update(entry.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(repr(entry.identity).encode("ascii"))
        digest.update(b"\0")
        if entry is layout.manifest:
            digest.update(sha256(manifest_raw).digest())
        digest.update(b"\0")
    # Reparse the loaded prefix so metadata and complete bytes cannot disagree.
    delimiter = content.find("\n---", 4)
    if delimiter < 0:
        raise SkillConfigurationError("Skill 配置不可用。")
    prefix = content[: delimiter + 4].encode("utf-8")
    if _parse_front_matter(prefix, directory_name=name) != layout.metadata:
        raise SkillConfigurationError("Skill 配置不可用。")
    for directory, expected in layout.directories:
        if _identity(_lstat(directory)) != expected:
            raise SkillConfigurationError("Skill 配置不可用。")
    return layout, _LoadedSnapshot(
        digest=digest.hexdigest(),
        content=content,
        resources=tuple(entry.relative_path for entry in layout.resources),
    )


class SkillRuntime:
    """Session-bound token store for Skill tools."""

    def __init__(self, *, session_id: str, root: Path | None = None) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise SkillConfigurationError("Skill 配置不可用。")
        self._session_id = session_id
        self._root = SKILLS_ROOT if root is None else Path(root)
        self._catalog = discover_skills(self._root)
        self._catalog_by_name = {item.name: item for item in self._catalog}
        self._tokens: dict[str, _TokenBinding] = {}

    @property
    def catalog(self) -> tuple[SkillMetadata, ...]:
        return self._catalog

    def load(self, arguments: LoadSkillArguments, *, model_step: int) -> LoadSkillOutput:
        if type(model_step) is not int or model_step < 0:
            raise SkillToolDenied("Skill runtime 未绑定模型步骤。")
        expected = self._catalog_by_name.get(arguments.skill_name)
        if expected is None:
            raise SkillToolDenied("Skill 不在启动目录中。")
        try:
            current = {item.name: item for item in discover_skills(self._root)}
            if current.get(arguments.skill_name) != expected:
                raise SkillConfigurationError("Skill 配置不可用。")
            layout, snapshot = _load_snapshot(self._root, arguments.skill_name)
            if layout.metadata != expected:
                raise SkillConfigurationError("Skill 配置不可用。")
        except SkillConfigurationError:
            raise SkillToolDenied("Skill 已变化或无法安全加载。") from None
        for token, binding in tuple(self._tokens.items()):
            if binding.skill_name == arguments.skill_name:
                self._tokens.pop(token, None)
        token = secrets.token_hex(32)
        self._tokens[token] = _TokenBinding(
            skill_name=arguments.skill_name,
            digest=snapshot.digest,
            loaded_step=model_step,
        )
        return LoadSkillOutput(
            name=expected.name,
            description=expected.description,
            content=snapshot.content,
            resources=list(snapshot.resources),
            skill_token=token,
        )

    def read_resource(
        self, arguments: ReadSkillResourceArguments, *, model_step: int
    ) -> ReadSkillResourceOutput:
        if type(model_step) is not int or model_step < 0:
            raise SkillToolDenied("Skill runtime 未绑定模型步骤。")
        binding = self._tokens.get(arguments.skill_token)
        if (
            binding is None
            or binding.skill_name != arguments.skill_name
            or model_step <= binding.loaded_step
        ):
            raise SkillToolDenied("Skill token 无效或尚未进入后续模型步骤。")
        try:
            layout, snapshot = _load_snapshot(self._root, arguments.skill_name)
        except SkillConfigurationError:
            self._tokens.pop(arguments.skill_token, None)
            raise SkillToolDenied("Skill 已变化，请重新加载。") from None
        if snapshot.digest != binding.digest or arguments.resource_path not in snapshot.resources:
            self._tokens.pop(arguments.skill_token, None)
            raise SkillToolDenied("Skill 已变化或资源路径无效，请重新加载。")
        resource = next(
            entry for entry in layout.resources
            if entry.relative_path == arguments.resource_path
        )
        try:
            raw = _read_exact_file(resource, maximum=_MAX_RESOURCE_BYTES)
            _decode_text(raw)
        except SkillConfigurationError:
            self._tokens.pop(arguments.skill_token, None)
            raise SkillToolDenied("Skill 资源无法作为安全 UTF-8 文本读取。") from None
        if arguments.offset > len(raw):
            raise SkillToolDenied("资源 offset 超出范围。")
        end = min(len(raw), arguments.offset + arguments.max_bytes)
        while end > arguments.offset:
            try:
                content = raw[arguments.offset:end].decode("utf-8")
            except UnicodeDecodeError:
                end -= 1
            else:
                break
        else:
            if arguments.offset == len(raw):
                content = ""
            else:
                raise SkillToolDenied("资源 offset 不是 UTF-8 字符边界。")
        bytes_read = end - arguments.offset
        return ReadSkillResourceOutput(
            name=arguments.skill_name,
            path=arguments.resource_path,
            encoding="utf-8",
            content=content,
            offset=arguments.offset,
            bytes_read=bytes_read,
            total_bytes=len(raw),
            truncated=end < len(raw),
        )
