"""Minimal VT100/xterm terminal emulator used by RKDesk.

Only stdlib is required.  It implements the subset of ANSI escape codes that
interactive BusyBox/Linux shells and common TUI programs actually emit.
"""

from __future__ import annotations

import codecs
import unicodedata
from collections import deque

DEFAULT_FG = (208, 222, 233)
DEFAULT_BG = (10, 15, 20)

# xterm's first 16 colours.
PALETTE = [
    (0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
    (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
    (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
    (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255),
]


def colour_256(index: int) -> tuple[int, int, int]:
    index = max(0, min(255, index))
    if index < 16:
        return PALETTE[index]
    if index < 232:
        index -= 16
        r, g, b = index // 36, (index % 36) // 6, index % 6
        return (0 if r == 0 else 55 + r * 40,
                0 if g == 0 else 55 + g * 40,
                0 if b == 0 else 55 + b * 40)
    level = 8 + (index - 232) * 10
    return (level, level, level)


def char_width(ch: str) -> int:
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


class Terminal:
    """A character grid with scrollback and an ANSI parser."""

    def __init__(self, cols: int = 90, rows: int = 28) -> None:
        self.cols = max(20, cols)
        self.rows = max(6, rows)
        self.scrollback: deque[list] = deque(maxlen=3000)
        self.lines = [self._blank_row() for _ in range(self.rows)]
        self.cx = 0
        self.cy = 0
        self.fg = DEFAULT_FG
        self.bg = DEFAULT_BG
        self.bold = False
        self.inverse = False
        self.underline = False
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self.saved_cursor: tuple[int, int] | None = None
        self.wrap_pending = False
        self.offset = 0
        self.title = ""
        self.cursor_visible = True
        self.alt_screen = False
        self._main_lines: list | None = None
        self._main_cursor = (0, 0)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "ground"
        self._csi = ""
        self._osc = ""
        self.respond = None  # callable(bytes) -> None, set by the server
        self.dirty = True

    # ------------------------------------------------------------------ grid
    def _blank_row(self) -> list:
        return [[" ", DEFAULT_FG, DEFAULT_BG, False, 1] for _ in range(self.cols)]

    def _blank_cell(self) -> list:
        return [" ", self.fg, self.bg, self.bold, 1]

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, cols), max(6, rows)
        if cols == self.cols and rows == self.rows:
            return
        for row in self.lines:
            if cols < len(row):
                del row[cols:]
            else:
                while len(row) < cols:
                    row.append([" ", DEFAULT_FG, DEFAULT_BG, False, 1])
        if rows < len(self.lines):
            removed = self.lines[: len(self.lines) - rows]
            if not self.alt_screen:
                self.scrollback.extend(removed)
            self.lines = self.lines[-rows:]
        else:
            self.lines.extend(self._blank_row() for _ in range(rows - len(self.lines)))
        self.cols, self.rows = cols, rows
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.cx = min(self.cx, cols - 1)
        self.cy = min(self.cy, rows - 1)
        self.wrap_pending = False
        self.offset = 0
        self.dirty = True

    def clear(self) -> None:
        self.lines = [self._blank_row() for _ in range(self.rows)]
        self.cx = self.cy = 0
        self.offset = 0
        self.dirty = True

    # --------------------------------------------------------------- parsing
    def feed(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        for ch in text:
            self._char(ch)

    def _char(self, ch: str) -> None:
        state = self._state
        if state == "ground":
            self._ground(ch)
        elif state == "esc":
            self._escape(ch)
        elif state == "csi":
            code = ord(ch)
            if 0x40 <= code <= 0x7E:
                self._csi_dispatch(self._csi, ch)
                self._csi = ""
                self._state = "ground"
            elif code >= 0x20:
                self._csi += ch
                if len(self._csi) > 128:
                    self._csi = ""
                    self._state = "ground"
        elif state == "osc":
            if ch == "\x07":
                self._osc_done()
            elif ch == "\x1b":
                self._state = "osc_esc"
            else:
                self._osc += ch
                if len(self._osc) > 1024:
                    self._osc = ""
                    self._state = "ground"
        elif state == "osc_esc":
            if ch == "\\":
                self._osc_done()
            else:
                self._osc += "\\" + ch
                self._state = "osc"

    def _osc_done(self) -> None:
        payload = self._osc
        self._osc = ""
        self._state = "ground"
        if ";" in payload:
            kind, value = payload.split(";", 1)
            if kind in ("0", "2") and value:
                self.title = value[:120]
                self.dirty = True

    def _ground(self, ch: str) -> None:
        if ch == "\x1b":
            self._state = "esc"
        elif ch == "\r":
            self.cx = 0
            self.wrap_pending = False
        elif ch == "\n":
            self._linefeed()
            self.wrap_pending = False
        elif ch == "\b":
            self.cx = max(0, self.cx - 1)
            self.wrap_pending = False
        elif ch == "\t":
            self.cx = min(self.cols - 1, (self.cx // 8 + 1) * 8)
            self.wrap_pending = False
        elif ch in ("\x00", "\x07", "\x0e", "\x0f"):
            pass
        elif ord(ch) < 0x20:
            pass
        else:
            self._put(ch)

    def _escape(self, ch: str) -> None:
        self._state = "ground"
        if ch == "[":
            self._state = "csi"
            self._csi = ""
        elif ch == "]":
            self._state = "osc"
            self._osc = ""
        elif ch == "7":
            self.saved_cursor = (self.cx, self.cy)
        elif ch == "8":
            if self.saved_cursor:
                self.cx, self.cy = self.saved_cursor
        elif ch == "D":
            self._linefeed()
        elif ch == "M":
            self._reverse_index()
        elif ch == "c":
            self.reset()
        elif ch in ("(", ")", "*", "+"):
            self._state = "charset"
        elif ch == "#":
            self._state = "charset"
        # Other single-character escapes are safe to ignore.

    def _charset(self, ch: str) -> None:  # consume one byte
        self._state = "ground"

    def _csi_dispatch(self, raw: str, final: str) -> None:
        private = raw[:1] in ("?", "<", "=", ">")
        body = raw[1:] if private else raw
        params = [int(p) if p else 0 for p in body.split(";")] if body else []

        def param(index: int, default: int = 1) -> int:
            try:
                value = params[index]
            except IndexError:
                return default
            return value if value else default

        if private:
            if final in ("h", "l") and params:
                enable = final == "h"
                for mode in params:
                    if mode == 25:
                        self.cursor_visible = enable
                    elif mode in (47, 1047, 1049):
                        self._set_alt(enable)
                self.dirty = True
            return

        if final == "A":
            self.cy = max(self.scroll_top, self.cy - param(0))
        elif final == "B":
            self.cy = min(self.scroll_bottom, self.cy + param(0))
        elif final == "C":
            self.cx = min(self.cols - 1, self.cx + param(0))
        elif final == "D":
            self.cx = max(0, self.cx - param(0))
        elif final == "E":
            self.cy = min(self.scroll_bottom, self.cy + param(0))
            self.cx = 0
        elif final == "F":
            self.cy = max(self.scroll_top, self.cy - param(0))
            self.cx = 0
        elif final in ("G", "`"):
            self.cx = min(self.cols - 1, max(0, param(0) - 1))
        elif final in ("H", "f"):
            self.cy = min(self.rows - 1, max(0, param(0) - 1))
            self.cx = min(self.cols - 1, max(0, param(1) - 1))
        elif final == "d":
            self.cy = min(self.rows - 1, max(0, param(0) - 1))
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "L":
            self._insert_lines(param(0))
        elif final == "M":
            self._delete_lines(param(0))
        elif final == "P":
            self._delete_chars(param(0))
        elif final == "@":
            self._insert_chars(param(0))
        elif final == "X":
            self._erase_chars(param(0))
        elif final == "S":
            for _ in range(param(0)):
                self._scroll_up(1)
        elif final == "T":
            for _ in range(param(0)):
                self._scroll_down(1)
        elif final == "m":
            self._sgr(params)
        elif final == "r":
            top = max(0, param(0) - 1)
            bottom = min(self.rows - 1, (params[1] - 1) if len(params) > 1 and params[1] else self.rows - 1)
            if bottom > top:
                self.scroll_top, self.scroll_bottom = top, bottom
                self.cx, self.cy = 0, top
        elif final == "s":
            self.saved_cursor = (self.cx, self.cy)
        elif final == "u":
            if self.saved_cursor:
                self.cx, self.cy = self.saved_cursor
        elif final == "n":
            if params and params[0] == 6 and self.respond:
                self.respond(f"\x1b[{self.cy + 1};{self.cx + 1}R".encode())
            elif params and params[0] == 5 and self.respond:
                self.respond(b"\x1b[0n")
        elif final == "c":
            if self.respond:
                self.respond(b"\x1b[?1;2c")
        elif final == "g":
            pass
        self.dirty = True

    def _sgr(self, params: list[int]) -> None:
        if not params:
            params = [0]
        index = 0
        while index < len(params):
            value = params[index]
            if value == 0:
                self.fg, self.bg = DEFAULT_FG, DEFAULT_BG
                self.bold = self.inverse = self.underline = False
            elif value == 1:
                self.bold = True
            elif value == 3:
                self.underline = True
            elif value == 4:
                self.underline = True
            elif value == 7:
                self.inverse = True
            elif value in (22, 23, 24):
                if value == 22:
                    self.bold = False
                else:
                    self.underline = False
            elif value == 27:
                self.inverse = False
            elif 30 <= value <= 37:
                self.fg = PALETTE[value - 30]
            elif value == 39:
                self.fg = DEFAULT_FG
            elif 40 <= value <= 47:
                self.bg = PALETTE[value - 40]
            elif value == 49:
                self.bg = DEFAULT_BG
            elif 90 <= value <= 97:
                self.fg = PALETTE[value - 90 + 8]
            elif 100 <= value <= 107:
                self.bg = PALETTE[value - 100 + 8]
            elif value in (38, 48) and index + 1 < len(params):
                target = "fg" if value == 38 else "bg"
                mode = params[index + 1]
                if mode == 5 and index + 2 < len(params):
                    colour = colour_256(params[index + 2])
                    index += 2
                elif mode == 2 and index + 4 < len(params):
                    colour = (params[index + 2], params[index + 3], params[index + 4])
                    index += 4
                else:
                    colour = None
                    index += 1
                if colour:
                    setattr(self, target, colour)
            index += 1
        self.dirty = True

    # ------------------------------------------------------------- mutations
    def _put(self, ch: str) -> None:
        width = char_width(ch)
        if width == 0:
            # Combining mark: attach to the preceding glyph.
            row = self.lines[self.cy]
            col = self.cx - 1
            while col >= 0 and row[col][4] == 0:
                col -= 1
            if col >= 0:
                row[col][0] += ch
            return
        if self.wrap_pending:
            self._linefeed()
            self.cx = 0
            self.wrap_pending = False
        if self.cx + width > self.cols:
            self._linefeed()
            self.cx = 0
        row = self.lines[self.cy]
        self._clear_wide_at(row, self.cx)
        if width == 2:
            self._clear_wide_at(row, self.cx + 1)
        fg, bg = (self.bg, self.fg) if self.inverse else (self.fg, self.bg)
        row[self.cx] = [ch, fg, bg, self.bold, width]
        if width == 2:
            row[self.cx + 1] = ["", fg, bg, self.bold, 0]
        self.cx += width
        if self.cx >= self.cols:
            self.cx = self.cols - 1
            self.wrap_pending = True
        self.dirty = True

    @staticmethod
    def _clear_wide_at(row: list, col: int) -> None:
        if col < 0 or col >= len(row):
            return
        cell = row[col]
        if cell[4] == 0 and col > 0:
            row[col - 1] = [" ", DEFAULT_FG, DEFAULT_BG, False, 1]
        elif cell[4] == 2 and col + 1 < len(row):
            row[col + 1] = [" ", DEFAULT_FG, DEFAULT_BG, False, 1]

    def _linefeed(self) -> None:
        if self.cy == self.scroll_bottom:
            self._scroll_up(1)
        else:
            self.cy = min(self.rows - 1, self.cy + 1)

    def _reverse_index(self) -> None:
        if self.cy == self.scroll_top:
            self._scroll_down(1)
        else:
            self.cy = max(0, self.cy - 1)

    def _scroll_up(self, count: int) -> None:
        for _ in range(count):
            if self.scroll_top == 0 and not self.alt_screen:
                self.scrollback.append(self.lines.pop(0))
            else:
                self.lines.pop(self.scroll_top)
            self.lines.insert(self.scroll_bottom, self._blank_row())
        self.dirty = True

    def _scroll_down(self, count: int) -> None:
        for _ in range(count):
            self.lines.pop(self.scroll_bottom)
            self.lines.insert(self.scroll_top, self._blank_row())
        self.dirty = True

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            for row in range(self.cy + 1, self.rows):
                self.lines[row] = self._blank_row()
        elif mode == 1:
            for row in range(0, self.cy):
                self.lines[row] = self._blank_row()
            self._erase_line(1)
        else:
            for row in range(self.rows):
                self.lines[row] = self._blank_row()
            if mode == 3:
                self.scrollback.clear()
        self.dirty = True

    def _erase_line(self, mode: int) -> None:
        row = self.lines[self.cy]
        start, end = (0, self.cx + 1) if mode == 1 else ((self.cx, self.cols) if mode == 0 else (0, self.cols))
        for col in range(start, end):
            row[col] = self._blank_cell()
        self.dirty = True

    def _insert_lines(self, count: int) -> None:
        for _ in range(count):
            if self.scroll_bottom < self.rows:
                self.lines.pop(self.scroll_bottom)
                self.lines.insert(self.cy, self._blank_row())
        self.dirty = True

    def _delete_lines(self, count: int) -> None:
        for _ in range(count):
            self.lines.pop(self.cy)
            self.lines.insert(self.scroll_bottom, self._blank_row())
        self.dirty = True

    def _delete_chars(self, count: int) -> None:
        row = self.lines[self.cy]
        for _ in range(min(count, self.cols - self.cx)):
            row.pop(self.cx)
            row.append(self._blank_cell())
        self.dirty = True

    def _insert_chars(self, count: int) -> None:
        row = self.lines[self.cy]
        for _ in range(min(count, self.cols - self.cx)):
            row.insert(self.cx, self._blank_cell())
            row.pop()
        self.dirty = True

    def _erase_chars(self, count: int) -> None:
        row = self.lines[self.cy]
        for col in range(self.cx, min(self.cols, self.cx + count)):
            row[col] = self._blank_cell()
        self.dirty = True

    def _set_alt(self, enable: bool) -> None:
        if enable and not self.alt_screen:
            self._main_lines = self.lines
            self._main_cursor = (self.cx, self.cy)
            self.lines = [self._blank_row() for _ in range(self.rows)]
            self.alt_screen = True
            self.cx = self.cy = 0
        elif not enable and self.alt_screen:
            self.lines = self._main_lines or [self._blank_row() for _ in range(self.rows)]
            self.cx, self.cy = self._main_cursor
            self.alt_screen = False
            self._main_lines = None
        self.dirty = True

    def reset(self) -> None:
        self.fg, self.bg = DEFAULT_FG, DEFAULT_BG
        self.bold = self.inverse = self.underline = False
        self.scroll_top, self.scroll_bottom = 0, self.rows - 1
        self.clear()

    # ------------------------------------------------------------- scrolling
    def scroll(self, delta: int) -> None:
        maximum = len(self.scrollback)
        self.offset = max(0, min(maximum, self.offset + delta))
        self.dirty = True

    def visible_rows(self) -> list:
        if self.offset <= 0:
            return self.lines
        back = list(self.scrollback)[-self.offset:]
        return back + self.lines[: max(0, self.rows - len(back))]
