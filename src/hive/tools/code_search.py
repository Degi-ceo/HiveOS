"""Bounded, deterministic source search for HiveOS Python code.

The index is process-local. It retains parsed lines and symbol locations only in
memory, refreshes changed files on demand, and never writes source to a database.
"""

from __future__ import annotations

import ast
import hashlib
import os
import stat
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Iterator

_MAX_FILE_BYTES = 1_000_000
_MAX_FILES = 5_000
_MAX_WALK_ENTRIES = 20_000
_MAX_WALK_DIRS = 1_000
_MAX_QUERY_LENGTH = 256
_MAX_RESULTS = 100
_MAX_CONTEXT_LINES = 5
_MAX_CONTEXT_LINE_LENGTH = 240
_MAX_CACHE_BYTES = 48 * 1024 * 1024
_MANIFEST_TTL_SECONDS = 5.0
_VERIFY_FILES_PER_REFRESH = 64


@dataclass(frozen=True)
class _Symbol:
    line: int
    column: int
    kind: str
    name: str


@dataclass(frozen=True)
class _IndexedFile:
    signature: tuple[int, int, int, int]
    digest: bytes
    cache_bytes: int
    lines: tuple[str, ...]
    symbols: tuple[_Symbol, ...]


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    # Windows lstat/fstat can disagree on ctime for the same open file.
    return (info.st_mtime_ns, info.st_size, info.st_dev, info.st_ino)


def _is_linklike(path: Path, info: os.stat_result) -> bool:
    """Reject links and Windows reparse points, including directory junctions."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


class CodeIndex:
    """Search only ``src/hive`` and ``tests`` beneath a repository root.

    Text matching is case-insensitive substring matching. Symbol matching is
    case-insensitive exact identifier matching for class/function definitions
    and direct or attribute calls. Returned files are repo-relative POSIX paths.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._cache: OrderedDict[str, _IndexedFile] = OrderedDict()
        self._cache_bytes = 0
        self._manifest: list[tuple[str, Path, tuple[int, int, int, int]]] = []
        self._next_manifest_refresh = 0.0
        self._verify_cursor = 0
        self._manifest_ready = False
        self._lock = threading.RLock()

    def refresh(self, *, force: bool = False) -> None:
        """Refresh the file manifest; ``force=True`` verifies every cached file.

        Ordinary searches use a five-second manifest TTL and verify at most 64
        cached files per periodic refresh. With the 5,000-file traversal cap,
        unchanged-metadata replacements can take up to 79 refresh cycles
        (about 395 seconds under continuous queries) to be noticed. Sparse
        queries extend that wall-clock window. Call ``refresh(force=True)``
        before critical diagnosis.
        """
        with self._lock:
            self._refresh_manifest(force=force)

    def search_text(self, query: str, *, limit: int = 10, context_lines: int = 1) -> list[dict]:
        """Return bounded text hits in stable file/line order."""
        if not self._valid_query(query):
            return []
        result_limit = self._bound(limit, _MAX_RESULTS)
        if result_limit == 0:
            return []
        context = self._bound(context_lines, _MAX_CONTEXT_LINES)
        needle = query.casefold()
        hits: list[dict] = []
        with closing(self._entries()) as entries:
            for relative, entry in entries:
                for line_number, line in enumerate(entry.lines, 1):
                    column = self._folded_match_column(line, needle)
                    if column < 0:
                        continue
                    hits.append({
                        "file": relative,
                        "line": line_number,
                        "context": self._context(
                            entry.lines, line_number, context,
                            match_column=column, match_length=len(query),
                        ),
                    })
                    if len(hits) >= result_limit:
                        return hits
        return hits

    def search_symbol(self, query: str, *, limit: int = 10, context_lines: int = 1) -> list[dict]:
        """Return matching definitions and call sites in stable file/line order."""
        if not self._valid_query(query):
            return []
        result_limit = self._bound(limit, _MAX_RESULTS)
        if result_limit == 0:
            return []
        context = self._bound(context_lines, _MAX_CONTEXT_LINES)
        needle = query.casefold()
        definitions: list[dict] = []
        calls: list[dict] = []
        with closing(self._entries()) as entries:
            for relative, entry in entries:
                for symbol in entry.symbols:
                    if symbol.name.casefold() != needle:
                        continue
                    target = definitions if symbol.kind == "definition" else calls
                    if len(target) >= result_limit:
                        continue
                    target.append({
                        "file": relative,
                        "line": symbol.line,
                        "context": self._context(
                            entry.lines, symbol.line, context,
                            match_column=symbol.column, match_length=len(symbol.name),
                        ),
                        "kind": symbol.kind,
                        "symbol": symbol.name,
                    })
        # Reserve capacity for definitions, then restore source order in output.
        hits = definitions + calls[: result_limit - len(definitions)]
        hits.sort(key=lambda hit: (hit["file"], hit["line"], hit["kind"] != "definition"))
        return hits

    @staticmethod
    def _valid_query(query: str) -> bool:
        return isinstance(query, str) and bool(query.strip()) and len(query) <= _MAX_QUERY_LENGTH

    @staticmethod
    def _folded_match_column(line: str, folded_query: str) -> int:
        folded_position = line.casefold().find(folded_query)
        if folded_position < 0:
            return -1
        consumed = 0
        for column, character in enumerate(line):
            consumed += len(character.casefold())
            if folded_position < consumed:
                return column
        return -1

    @staticmethod
    def _bound(value: int, maximum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("limit and context_lines must be integers")
        return min(max(value, 0), maximum)

    @staticmethod
    def _context(
        lines: tuple[str, ...], line_number: int, surrounding: int,
        *, match_column: int | None = None, match_length: int = 0,
    ) -> str:
        start = max(0, line_number - 1 - surrounding)
        stop = min(len(lines), line_number + surrounding)
        rendered: list[str] = []
        for position in range(start, stop):
            line = lines[position]
            width = max(_MAX_CONTEXT_LINE_LENGTH, match_length if position + 1 == line_number else 0)
            offset = 0
            if position + 1 == line_number and match_column is not None:
                offset = min(max(0, match_column - width // 3), max(0, len(line) - width))
            excerpt = line[offset:offset + width]
            if offset:
                excerpt = "…" + excerpt
            if offset + width < len(line):
                excerpt += "…"
            rendered.append(f"{position + 1}: {excerpt}")
        return "\n".join(rendered)

    def _entries(self) -> Iterator[tuple[str, _IndexedFile]]:
        """Stream indexed files while keeping only a bounded LRU cache."""
        with self._lock:
            self._refresh_manifest(force=False)
            for relative, path, _ in self._manifest:
                # A cached path may have acquired an intermediate junction or
                # symlink since the last manifest walk. Never yield it blindly.
                if not self._safe_file(path):
                    self._forget(relative)
                    continue
                entry = self._cache.get(relative)
                if entry is None:
                    entry = self._load(relative, path)
                if entry is None:
                    self._forget(relative)
                else:
                    self._remember(relative, entry)
                    yield relative, entry

    def _refresh_manifest(self, *, force: bool) -> None:
        now = time.monotonic()
        if self._manifest_ready and not force and now < self._next_manifest_refresh:
            return
        candidates: list[tuple[str, Path, tuple[int, int, int, int]]] = []
        for relative, path in self._paths()[:_MAX_FILES]:
            try:
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or _is_linklike(path, info)
                    or info.st_nlink != 1
                    or info.st_size > _MAX_FILE_BYTES
                    or not self._inside_allowed_tree(path)
                ):
                    continue
            except (OSError, RuntimeError):
                continue
            candidates.append((relative, path, _signature(info)))
        signatures = {relative: signature for relative, _, signature in candidates}
        for relative, entry in tuple(self._cache.items()):
            if signatures.get(relative) != entry.signature:
                self._forget(relative)
        self._manifest = candidates
        self._manifest_ready = True
        self._next_manifest_refresh = now + _MANIFEST_TTL_SECONDS
        if not candidates:
            self._verify_cursor = 0
            return
        count = len(candidates) if force else min(_VERIFY_FILES_PER_REFRESH, len(candidates))
        for offset in range(count):
            relative, path, _ = candidates[(self._verify_cursor + offset) % len(candidates)]
            if relative not in self._cache:
                continue
            entry = self._load(relative, path)
            if entry is None:
                self._forget(relative)
            else:
                self._remember(relative, entry)
        self._verify_cursor = (self._verify_cursor + count) % len(candidates)

    def _forget(self, relative: str) -> None:
        previous = self._cache.pop(relative, None)
        if previous is not None:
            self._cache_bytes -= previous.cache_bytes

    def _remember(self, relative: str, entry: _IndexedFile) -> None:
        self._forget(relative)
        if entry.cache_bytes > _MAX_CACHE_BYTES:
            return
        while self._cache and self._cache_bytes + entry.cache_bytes > _MAX_CACHE_BYTES:
            oldest, _ = next(iter(self._cache.items()))
            self._forget(oldest)
        self._cache[relative] = entry
        self._cache_bytes += entry.cache_bytes

    def _paths(self) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        visited_entries = 0
        visited_directories = 0
        for base in (self.root / "src" / "hive", self.root / "tests"):
            if not self._safe_directory(base):
                continue
            pending = deque([base])
            while pending and visited_entries < _MAX_WALK_ENTRIES and visited_directories < _MAX_WALK_DIRS:
                parent = pending.popleft()
                visited_directories += 1
                remaining = _MAX_WALK_ENTRIES - visited_entries
                try:
                    with os.scandir(parent) as scanner:
                        # Stop consuming the directory stream at the global cap.
                        # A pathological tree can yield a deliberately partial index.
                        names = sorted(entry.name for entry in islice(scanner, remaining))
                except OSError:
                    continue
                visited_entries += len(names)
                for name in names:
                    path = parent / name
                    if name != "__pycache__" and self._safe_directory(path):
                        pending.append(path)
                    elif name.endswith(".py") and self._safe_file(path):
                        found.append((path.relative_to(self.root).as_posix(), path))
                        if len(found) >= _MAX_FILES:
                            break
                if len(found) >= _MAX_FILES:
                    break
            if visited_entries >= _MAX_WALK_ENTRIES or visited_directories >= _MAX_WALK_DIRS or len(found) >= _MAX_FILES:
                break
        found.sort(key=lambda item: item[0])
        return found

    def _inside_allowed_tree(self, path: Path) -> bool:
        """Reject aliases at every component and keep the resolved target in its tree."""
        try:
            relative = path.relative_to(self.root)
            parts = relative.parts
            if not parts or any(part in {".", ".."} for part in parts):
                return False
            if len(parts) >= 2 and parts[:2] == ("src", "hive"):
                allowed = self.root / "src" / "hive"
            elif parts[0] == "tests":
                allowed = self.root / "tests"
            else:
                return False
            current = self.root
            for position, part in enumerate(parts):
                current = current / part
                info = current.lstat()
                if _is_linklike(current, info):
                    return False
                if position < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                    return False
            return path.resolve(strict=True).is_relative_to(allowed)
        except (OSError, RuntimeError, ValueError):
            return False

    def _safe_directory(self, path: Path) -> bool:
        try:
            info = path.lstat()
            return stat.S_ISDIR(info.st_mode) and not _is_linklike(path, info) and self._inside_allowed_tree(path)
        except OSError:
            return False

    def _safe_file(self, path: Path) -> bool:
        try:
            info = path.lstat()
            return (
                stat.S_ISREG(info.st_mode)
                and not _is_linklike(path, info)
                and info.st_nlink == 1
                and info.st_size <= _MAX_FILE_BYTES
                and self._inside_allowed_tree(path)
            )
        except OSError:
            return False

    def _load(self, relative: str, path: Path) -> _IndexedFile | None:
        try:
            before = path.lstat()
            if _is_linklike(path, before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            if before.st_size > _MAX_FILE_BYTES or not self._inside_allowed_tree(path):
                return None
            previous = self._cache.get(relative)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > _MAX_FILE_BYTES:
                    return None
                chunks: list[bytes] = []
                remaining = _MAX_FILE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if len(content) > _MAX_FILE_BYTES:
                    return None
                after_opened = os.fstat(descriptor)
                after_path = path.lstat()
                if (
                    _signature(opened) != _signature(before)
                    or _signature(after_opened) != _signature(before)
                    or _signature(after_path) != _signature(before)
                    or _is_linklike(path, after_path)
                    or not self._inside_allowed_tree(path)
                ):
                    return None
            finally:
                os.close(descriptor)
        except (OSError, RuntimeError):
            return None
        digest = hashlib.blake2b(content, digest_size=16).digest()
        if previous is not None and previous.signature == _signature(before) and previous.digest == digest:
            return previous
        source = content.decode("utf-8", errors="replace")
        lines = tuple(source.splitlines())
        try:
            tree = ast.parse(source, filename=relative)
        except (SyntaxError, ValueError):
            symbols: tuple[_Symbol, ...] = ()
        else:
            found: list[_Symbol] = []
            encoded_lines: dict[int, bytes] = {}

            def character_column(line_number: int, byte_column: int) -> int:
                encoded = encoded_lines.get(line_number)
                if encoded is None:
                    encoded = lines[line_number - 1].encode("utf-8")
                    encoded_lines[line_number] = encoded
                return len(encoded[:byte_column].decode("utf-8", errors="ignore"))

            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = character_column(node.lineno, node.col_offset)
                    column = lines[node.lineno - 1].find(node.name, start)
                    found.append(_Symbol(node.lineno, max(column, start), "definition", node.name))
                elif isinstance(node, ast.Call):
                    function = node.func
                    if isinstance(function, ast.Name):
                        column = character_column(function.lineno, function.col_offset)
                        found.append(_Symbol(function.lineno, column, "call", function.id))
                    elif isinstance(function, ast.Attribute):
                        line_number = function.end_lineno or function.lineno
                        end = character_column(line_number, function.end_col_offset or 0)
                        found.append(_Symbol(line_number, max(0, end - len(function.attr)), "call", function.attr))
            symbols = tuple(sorted(found, key=lambda item: (item.line, item.kind != "definition", item.name)))
        # Conservative accounting includes decoded strings, tuples, symbol
        # objects, cache keys and container overhead; raw source is not retained.
        cache_bytes = len(content) * 4 + len(lines) * 128 + len(symbols) * 192 + 512
        return _IndexedFile(_signature(opened), digest, cache_bytes, lines, symbols)
