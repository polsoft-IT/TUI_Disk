#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""TXDisk: interactive disk-usage browser for the terminal."""

try:
    import curses
    _CURSES_OK = True
except ModuleNotFoundError:
    _CURSES_OK = False

import os
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


_CP_TITLE = 1
_CP_STATUS = 2
_CP_SELECTED = 3
_CP_DIR = 4
_CP_FILE = 5
_CP_MAP = 6
_CP_MUTED = 7
_CP_ALERT = 8
_CP_DIVIDER = 9

_APP_NAME = "TX-Disk"
_APP_VERSION = "1.0.0"
_APP_LANG = "en"
_APP_DESCRIPTION = "Interactive disk-usage analyzer and directory browser."
_APP_AUTHOR = "Sebastian Januchowski"
_APP_COMPANY = "polsoft.ITS(TM)"
_APP_EMAIL = "polsoft.its@mail.com"
_APP_URL = "https://github.com/polsoft-IT"


@dataclass
class DiskEntry:
    name: str
    path: Path
    is_dir: bool
    size: int
    is_parent: bool = False
    # Fast metadata-scan fields (no file contents are ever opened)
    mtime: float = 0.0
    atime: float = 0.0
    inode: int = 0
    nlink: int = 1
    allocated: int = 0        # on-disk blocks (st_blocks*512) when known
    sparse: bool = False      # logical size >> allocated
    hardlink_dup: bool = False  # another entry already owns this inode


@dataclass
class ScanStats:
    """Aggregated metadata-scan results plus coverage reporting."""
    scanned: int = 0
    errors: int = 0
    logical_bytes: int = 0        # sum of st_size over unique files
    physical_bytes: int = 0       # sum of allocated blocks over unique files
    unique_files: int = 0
    hardlink_duplicates: int = 0  # entries sharing an already-seen inode
    hardlink_saved: int = 0       # logical bytes not double-counted
    sparse_files: int = 0
    sparse_saved: int = 0         # logical - allocated for sparse files
    coverage: List[str] = field(default_factory=list)  # what was NOT scanned

    def note_unscannable(self, path: str, reason: str,
                         limit: int = 200) -> None:
        """Anything the scan could not cover is reported as coverage
        instead of being silently dropped."""
        self.errors += 1
        if len(self.coverage) < limit:
            self.coverage.append(f"{path}  ({reason})")


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            if unit == "B":
                return f"{value:,.0f} {unit}"
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} PiB"


def scan_directory(
    root: Path,
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int], None] = lambda count: None,
) -> Tuple[List[DiskEntry], int, ScanStats]:
    """Fast metadata-only scan of a directory tree.

    Reads only stat information (size, type, mtime, atime, inode, link
    count, allocated blocks) via ``os.scandir`` — no file is ever opened
    for reading.  Hardlinks are deduplicated per (device, inode) so the
    reported unique on-disk bytes count each physical block once, sparse
    files are detected from logical vs allocated size, and everything the
    scan could not cover is reported in ``stats.coverage``.
    """
    stats = ScanStats()
    pending = [root]
    sizes = {root: 0}
    entries_by_directory = {}
    directories = {root}
    seen_inodes: Dict[Tuple[int, int], None] = {}

    while pending:
        if cancelled():
            return [], 0, stats
        current = pending.pop()
        children = []
        try:
            with os.scandir(current) as iterator:
                for item in iterator:
                    if cancelled():
                        return [], 0, stats
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError as exc:
                        stats.note_unscannable(item.path, os.strerror(
                            exc.errno) if exc.errno else type(exc).__name__)
                        continue
                    path = Path(item.path)
                    is_dir = stat.S_ISDIR(info.st_mode)
                    inode = getattr(info, "st_ino", 0) or 0
                    dev = getattr(info, "st_dev", 0) or 0
                    nlink = getattr(info, "st_nlink", 1) or 1
                    # Allocated on-disk bytes; st_blocks is POSIX-only.
                    blocks = getattr(info, "st_blocks", None)
                    if blocks is not None:
                        allocated = max(0, blocks) * 512
                    else:
                        allocated = info.st_size  # unknown on this platform
                    entry = DiskEntry(
                        item.name, path, is_dir,
                        0 if is_dir else info.st_size,
                        mtime=getattr(info, "st_mtime", 0.0),
                        atime=getattr(info, "st_atime", 0.0),
                        inode=inode,
                        nlink=nlink,
                        allocated=0 if is_dir else allocated,
                    )
                    children.append(entry)
                    if is_dir:
                        sizes[path] = 0
                        directories.add(path)
                        pending.append(path)
                    else:
                        sizes[current] += info.st_size
                        # Hardlink / sparse accounting (unique inodes only)
                        key = (dev, inode) if inode else None
                        if key is not None and key in seen_inodes:
                            entry.hardlink_dup = True
                            stats.hardlink_duplicates += 1
                            stats.hardlink_saved += info.st_size
                        else:
                            if key is not None:
                                seen_inodes[key] = None
                            stats.unique_files += 1
                            stats.logical_bytes += info.st_size
                            stats.physical_bytes += allocated
                            if info.st_size > allocated + 4096:
                                # Sparse: logical size much larger than the
                                # blocks actually stored on disk.
                                entry.sparse = True
                                stats.sparse_files += 1
                                stats.sparse_saved += (
                                    info.st_size - allocated
                                )
                    scanned = stats.scanned = stats.scanned + 1
                    if scanned % 64 == 0:
                        on_progress(scanned)
        except OSError as exc:
            stats.note_unscannable(str(current), os.strerror(
                exc.errno) if exc.errno else type(exc).__name__)
        entries_by_directory[current] = children

    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        if directory != root:
            parent = directory.parent
            sizes[parent] = sizes.get(parent, 0) + sizes[directory]

    root_entries = entries_by_directory.get(root, [])
    for entry in root_entries:
        if entry.is_dir:
            entry.size = sizes.get(entry.path, 0)
    on_progress(stats.scanned)
    return root_entries, sizes[root], stats


class TXDisk:
    def __init__(self, screen, root: Path) -> None:
        self.screen = screen
        self.path = root.expanduser().resolve()
        self.entries: List[DiskEntry] = []
        self.total_size = 0
        self.cursor = 0
        self.offset = 0
        self.scanning = False
        self.scanned = 0
        self.stats = ScanStats()
        self.error = ""
        self.notice = ""
        self.sort_by_size = True
        self.generation = 0
        self.lock = threading.Lock()
        self.running = True
        self._start_scan()

    def _sorted(self, entries: List[DiskEntry]) -> List[DiskEntry]:
        if self.sort_by_size:
            return sorted(
                entries,
                key=lambda item: (-item.size, item.name.casefold()),
            )
        return sorted(entries, key=lambda item: item.name.casefold())

    def _start_scan(self) -> None:
        self.generation += 1
        generation = self.generation
        with self.lock:
            self.entries = []
            self.total_size = 0
            self.scanned = 0
            self.stats = ScanStats()
            self.scanning = True
            self.error = ""
            self.notice = ""
        self.cursor = self.offset = 0

        def cancelled() -> bool:
            return generation != self.generation

        def progress(count: int) -> None:
            with self.lock:
                if generation == self.generation:
                    self.scanned = count

        def worker() -> None:
            try:
                entries, total, stats = scan_directory(
                    self.path, cancelled, progress
                )
            except OSError as exc:
                with self.lock:
                    if generation == self.generation:
                        self.error = str(exc)
                        self.scanning = False
                return
            with self.lock:
                if generation == self.generation:
                    self.entries = self._sorted(entries)
                    self.total_size = total
                    self.scanned = stats.scanned
                    self.stats = stats
                    self.scanning = False
                    self.cursor = (
                        1 if self.path.parent != self.path and entries else 0
                    )
                    self.offset = 0

        threading.Thread(target=worker, daemon=True).start()

    def _visible_entries(self) -> List[DiskEntry]:
        with self.lock:
            entries = list(self.entries)
        if self.path.parent != self.path:
            entries.insert(
                0, DiskEntry("..", self.path.parent, True, 0, is_parent=True)
            )
        return entries

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width:
            try:
                self.screen.addnstr(y, x, text, width - x, attr)
            except curses.error:
                pass

    def _disk_usage(self) -> Tuple[int, int, float]:
        try:
            usage = shutil.disk_usage(self.path)
            used = usage.total - usage.free
            percent = (used / usage.total * 100) if usage.total else 0.0
            return used, usage.total, percent
        except OSError:
            return 0, 0, 0.0

    def draw(self) -> None:
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        title_attr = curses.color_pair(_CP_TITLE) | curses.A_BOLD
        self._put(
            0, 0,
            f" {_APP_NAME} v{_APP_VERSION}  |  "
            "Disk usage explorer  |  F1: About  "
            f"{'SCANNING' if self.scanning else 'READY'} ".ljust(width),
            title_attr,
        )
        self._put(1, 0, f" Path: {self.path} ", curses.color_pair(_CP_DIR))
        with self.lock:
            entries = list(self.entries)
            total_size = self.total_size
            scanned = self.scanned
            stats = self.stats
            scanning = self.scanning
            error = self.error
            notice = self.notice
        self._put(
            2, 0,
            f" Folder total: {format_size(total_size)}  |  "
            f"{len(entries)} items  |  {scanned} scanned "
            f"| Unique: {format_size(stats.logical_bytes)} "
            f"(on-disk: {format_size(stats.physical_bytes)})"
            + (f" | hardlinks saved: {format_size(stats.hardlink_saved)}"
               if stats.hardlink_saved else "")
            + (f" | sparse: {stats.sparse_files} "
               f"({format_size(stats.sparse_saved)})"
               if stats.sparse_files else ""),
            curses.color_pair(_CP_MUTED),
        )

        used, capacity, percent = self._disk_usage()
        bar_width = max(4, width - 32)
        filled = min(bar_width, round(bar_width * percent / 100))
        volume_bar = "█" * filled + "░" * (bar_width - filled)
        volume_line = (
            f" Volume {format_size(used)} / {format_size(capacity)} "
            f"({percent:4.1f}%) {volume_bar}"
        )
        self._put(
            3, 0, volume_line,
            curses.color_pair(_CP_MAP) | curses.A_BOLD,
        )
        self._put(4, 0, "─" * width, curses.color_pair(_CP_DIVIDER))
        bar_width = min(12, max(4, width // 8))
        prefix_width = 6
        fixed_width = prefix_width + 1 + 9 + 1 + 6 + 1 + bar_width
        name_width = max(1, width - fixed_width)
        header = (
            f" TYPE {'NAME':<{name_width}} {'SIZE':>9} "
            f"{'SHARE':>6} {'MAP':<{bar_width}}"
        )
        self._put(5, 0, header, curses.color_pair(_CP_TITLE) | curses.A_BOLD)

        visible = self._visible_entries()
        list_top = 6
        list_rows = max(0, height - list_top - 2)
        self.offset = max(
            0, min(self.offset, max(0, len(visible) - list_rows))
        )
        for row in range(list_rows):
            index = self.offset + row
            if index >= len(visible):
                break
            item = visible[index]
            y = list_top + row
            selected = index == self.cursor
            row_attr = curses.color_pair(_CP_SELECTED) if selected else 0
            if selected:
                self._put(y, 0, " " * width, row_attr)
            if item.is_parent:
                kind = "UP"
            else:
                kind = "DIR" if item.is_dir else "FILE"
            kind_attr = (
                row_attr if selected else
                curses.color_pair(_CP_DIR if item.is_dir else _CP_FILE)
            )
            self._put(y, 0, ">" if selected else " ", row_attr)
            kind = kind.ljust(4)
            if not item.is_dir:
                flags = ("L" if item.hardlink_dup else " ") + (
                    "S" if item.sparse else " "
                )
                kind = kind[:2] + flags + " "
            self._put(y, 1, kind, kind_attr | curses.A_BOLD)
            name = item.name[:name_width].ljust(name_width)
            self._put(y, prefix_width, name, row_attr)
            size_x = prefix_width + name_width + 1
            share = item.size / total_size * 100 if total_size else 0.0
            data_attr = row_attr or curses.color_pair(_CP_MUTED)
            self._put(
                y, size_x, f"{format_size(item.size):>9}", data_attr
            )
            share_x = size_x + 10
            self._put(y, share_x, f"{share:5.1f}%", data_attr)
            blocks = 0
            if total_size and item.size:
                blocks = max(1, round(bar_width * share / 100))
            blocks = min(bar_width, blocks)
            map_bar = "█" * blocks + "░" * (bar_width - blocks)
            self._put(
                y, share_x + 7, map_bar,
                row_attr or curses.color_pair(_CP_MAP),
            )

        if not visible and not scanning:
            self._put(
                6, 2, "This folder is empty.",
                curses.color_pair(_CP_MUTED),
            )

        if error:
            status = error
            status_attr = curses.color_pair(_CP_ALERT)
        elif scanning:
            status = f"Scanning tree... {scanned} entries visited"
            status_attr = curses.color_pair(_CP_STATUS)
        elif stats.coverage:
            status = (
                f"Scan done; {stats.errors} item(s) NOT covered "
                "(F9: coverage report)"
            )
            status_attr = curses.color_pair(_CP_ALERT)
        elif notice:
            status = notice
            status_attr = curses.color_pair(_CP_STATUS)
        else:
            status = f"{len(entries)} items | {format_size(total_size)} total"
            status_attr = curses.color_pair(_CP_STATUS)
        self._put(height - 2, 0, f" {status} ".ljust(width), status_attr)
        footer = (
            " F1 About  F2 Refresh  F5 Delete  F9 Coverage  Enter Open  "
            "S Sort  Q/Esc Quit "
        ).ljust(width)
        self._put(
            height - 1, 0, footer, curses.color_pair(_CP_TITLE)
        )
        self.screen.refresh()

    def _move(self, delta: int) -> None:
        entries = self._visible_entries()
        self.cursor = max(0, min(len(entries) - 1, self.cursor + delta))
        height, _ = self.screen.getmaxyx()
        visible_rows = max(1, height - 8)
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + visible_rows:
            self.offset = self.cursor - visible_rows + 1

    def _current_entry(self) -> Optional[DiskEntry]:
        entries = self._visible_entries()
        if not entries:
            return None
        self.cursor = max(0, min(self.cursor, len(entries) - 1))
        return entries[self.cursor]

    def _coverage_report(self) -> None:
        """F9: show everything the scan could not cover."""
        with self.lock:
            coverage = list(self.stats.coverage)
            errors = self.stats.errors
        height, width = self.screen.getmaxyx()
        offset = 0
        self.screen.timeout(-1)
        try:
            while True:
                self.screen.erase()
                self._put(
                    0, 0,
                    f" Coverage report  |  {errors} item(s) not covered "
                    .ljust(width),
                    curses.color_pair(_CP_ALERT) | curses.A_BOLD,
                )
                rows = max(1, height - 3)
                if not coverage:
                    self._put(
                        2, 2,
                        "Nothing missing - the scan covered the whole tree.",
                        curses.color_pair(_CP_MUTED),
                    )
                for row in range(rows):
                    index = offset + row
                    if index >= len(coverage):
                        break
                    self._put(row + 1, 0, coverage[index][:width])
                self._put(
                    height - 1, 0,
                    " Up/Down: scroll | Esc/Enter: back ".ljust(width),
                    curses.color_pair(_CP_STATUS),
                )
                self.screen.refresh()
                key = self.screen.getch()
                if key in (27, 10, 13):
                    return
                elif key == curses.KEY_UP:
                    offset = max(0, offset - 1)
                elif key == curses.KEY_DOWN:
                    offset = min(max(0, len(coverage) - rows), offset + 1)
                elif key == curses.KEY_PPAGE:
                    offset = max(0, offset - rows)
                elif key == curses.KEY_NPAGE:
                    offset = min(max(0, len(coverage) - rows), offset + rows)
        finally:
            self.screen.timeout(100)

    def _about(self) -> None:
        height, width = self.screen.getmaxyx()
        lines = [
            f"{_APP_NAME} v{_APP_VERSION} ({_APP_LANG})",
            "",
            _APP_DESCRIPTION,
            "",
            f"Author:   {_APP_AUTHOR}",
            f"Company:  {_APP_COMPANY}",
            f"E-mail:   {_APP_EMAIL}",
            f"GitHub:   {_APP_URL}",
            "",
            "Press any key to return...",
        ]
        self.screen.erase()
        self._put(
            0, 0, f" About {_APP_NAME} ".ljust(width),
            curses.color_pair(_CP_TITLE) | curses.A_BOLD,
        )
        top = max(1, (height - len(lines)) // 2)
        for row, line in enumerate(lines):
            self._put(
                top + row, max(0, (width - len(line)) // 2), line,
                curses.color_pair(_CP_MUTED)
                if line.startswith("Press") else 0,
            )
        self._put(
            height - 1, 0, " Press any key to return ".ljust(width),
            curses.color_pair(_CP_STATUS),
        )
        self.screen.refresh()
        self.screen.timeout(-1)
        try:
            self.screen.get_wch()
        finally:
            self.screen.timeout(100)

    def _confirm_delete(self, entry: DiskEntry) -> bool:
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        title = " DELETE CONFIRMATION "
        self._put(
            0, 0, title.ljust(width),
            curses.color_pair(_CP_ALERT) | curses.A_BOLD,
        )
        warning = (
            "This directory and all its contents will be permanently removed."
            if entry.is_dir else "This file will be permanently removed."
        )
        self._put(2, 2, warning, curses.A_BOLD)
        self._put(4, 2, f"Path: {entry.path}")
        self._put(5, 2, f"Size: {format_size(entry.size)}")
        self._put(
            7, 2, "Press Y to delete permanently. Any other key cancels.",
            curses.color_pair(_CP_STATUS),
        )
        self.screen.refresh()
        self.screen.timeout(-1)
        try:
            key = self.screen.get_wch()
        finally:
            self.screen.timeout(100)
        return key in ("y", "Y", ord("y"), ord("Y"))

    def _delete_selected(self) -> None:
        if self.scanning:
            self.error = "Wait for the scan to finish before deleting"
            return
        entry = self._current_entry()
        if entry is None:
            self.error = "No item selected"
            return
        if entry.is_parent:
            self.error = "The parent directory cannot be deleted here"
            return
        if not self._confirm_delete(entry):
            self.error = "Deletion cancelled"
            return
        try:
            if entry.path.is_symlink():
                entry.path.unlink()
            elif entry.is_dir:
                shutil.rmtree(entry.path)
            else:
                entry.path.unlink()
        except OSError as exc:
            self.error = f"Delete failed: {exc}"
            return
        self.error = f"Deleted {entry.name}"
        self._start_scan()
        with self.lock:
            self.notice = f"Deleted {entry.name}"

    def _enter(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if entry.is_parent or entry.is_dir:
            self.path = entry.path.resolve()
            self._start_scan()
        else:
            self.error = "Select a directory to drill down"

    def run(self) -> None:
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        _init_colors()
        while self.running:
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if isinstance(key, str):
                if key in ("q", "Q", "\x1b"):
                    self.running = False
                elif key in ("s", "S"):
                    self.sort_by_size = not self.sort_by_size
                    with self.lock:
                        self.entries = self._sorted(self.entries)
                    self.cursor = self.offset = 0
                elif key in ("\n", "\r"):
                    self._enter()
            elif key == curses.KEY_UP:
                self._move(-1)
            elif key == curses.KEY_DOWN:
                self._move(1)
            elif key == curses.KEY_PPAGE:
                self._move(-max(1, self.screen.getmaxyx()[0] - 8))
            elif key == curses.KEY_NPAGE:
                self._move(max(1, self.screen.getmaxyx()[0] - 8))
            elif key == curses.KEY_HOME:
                self.cursor = self.offset = 0
            elif key == curses.KEY_END:
                self.cursor = max(0, len(self._visible_entries()) - 1)
                self._move(0)
            elif key == curses.KEY_F2:
                self._start_scan()
            elif key == curses.KEY_F1:
                self._about()
            elif key == curses.KEY_F9:
                self._coverage_report()
            elif key == curses.KEY_F5:
                self._delete_selected()


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(_CP_STATUS, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(_CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(_CP_DIR, curses.COLOR_GREEN, -1)
    curses.init_pair(_CP_FILE, curses.COLOR_WHITE, -1)
    curses.init_pair(_CP_MAP, curses.COLOR_YELLOW, -1)
    curses.init_pair(_CP_MUTED, curses.COLOR_CYAN, -1)
    curses.init_pair(_CP_ALERT, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(_CP_DIVIDER, curses.COLOR_CYAN, -1)


def main() -> int:
    if not _CURSES_OK:
        print(
            "TXDisk requires the curses module. On Windows, install with: "
            "pip install windows-curses",
            file=sys.stderr,
        )
        return 1
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.cwd()
    if not root.is_dir():
        print(f"TXDisk: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        curses.wrapper(lambda screen: TXDisk(screen, root).run())
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"TXDisk: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
