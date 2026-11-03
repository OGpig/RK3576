"""Virtual desktop rendered entirely on the RK3576.

The browser is a dumb display: the board paints every pixel, owns the window
manager, and applies mouse/keyboard input.  This keeps working on a headless
Buildroot image whose Weston compositor is unavailable.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque

from PIL import Image, ImageDraw, ImageFont

from .terminal import DEFAULT_BG, DEFAULT_FG, Terminal

WIDTH, HEIGHT = 1280, 800
TOPBAR = 36
DOCK = 46
TITLE = 32
RADIUS = 9

BG_TOP = (8, 12, 17)
BG_BOTTOM = (13, 21, 28)
GRID = (16, 25, 33)
PANEL = (17, 24, 32)
PANEL_DARK = (10, 15, 20)
TITLE_BG = (25, 36, 47)
TITLE_BG_IDLE = (19, 28, 37)
BORDER = (40, 56, 70)
BORDER_FOCUS = (47, 212, 167)
TEXT = (214, 226, 236)
TEXT_DIM = (125, 142, 158)
TEXT_FAINT = (86, 101, 115)
ACCENT = (47, 212, 167)
ACCENT_DARK = (22, 74, 62)
WARN = (240, 180, 80)
DANGER = (255, 95, 87)


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


FONT_DIR = "/usr/share/fonts"
MONO = _font(f"{FONT_DIR}/dejavu/DejaVuSansMono.ttf", 15)
MONO_BOLD = _font(f"{FONT_DIR}/dejavu/DejaVuSansMono-Bold.ttf", 15)
CJK = _font(f"{FONT_DIR}/source-han-sans-cn/SourceHanSansCN-Regular.otf", 15)
UI = _font(f"{FONT_DIR}/source-han-sans-cn/SourceHanSansCN-Regular.otf", 13)
UI_BOLD = _font(f"{FONT_DIR}/source-han-sans-cn/SourceHanSansCN-Bold.otf", 13)
SMALL = _font(f"{FONT_DIR}/source-han-sans-cn/SourceHanSansCN-Regular.otf", 11)
SMALL_MONO = _font(f"{FONT_DIR}/dejavu/DejaVuSansMono.ttf", 11)
BIG = _font(f"{FONT_DIR}/source-han-sans-cn/SourceHanSansCN-Bold.otf", 18)

CELL_W = max(8, int(round(MONO.getlength("M"))))
CELL_H = 19
TERM_PAD = 8

DOCK_ITEMS = [
    ("terminal", "终端", "T"),
    ("monitor", "性能", "P"),
    ("files", "文件", "F"),
    ("help", "帮助", "?"),
]

SPECIAL_KEYS = {
    "Enter": b"\r",
    "Backspace": b"\x7f",
    "Tab": b"\t",
    "Escape": b"\x1b",
    "ArrowUp": b"\x1b[A",
    "ArrowDown": b"\x1b[B",
    "ArrowRight": b"\x1b[C",
    "ArrowLeft": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "Delete": b"\x1b[3~",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "Insert": b"\x1b[2~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    "F5": b"\x1b[15~",
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
}

HELP_LINES = [
    ("RK3576 Remote Workbench", "title"),
    ("", ""),
    ("浏览器就是显示器：板端渲染画面，通过网络推流；鼠标和键盘事件回传板端。", ""),
    ("", ""),
    ("· 终端窗口内可直接输入命令，支持颜色、中文、滚动回看。", ""),
    ("· 拖动标题栏移动窗口；拖右下角调整大小；双击标题栏最大化。", ""),
    ("· 底部按钮切换窗口；F1~F4 快速打开帮助/终端/性能/文件。", ""),
    ("· 鼠标滚轮在终端内回看历史输出。", ""),
    ("· 工具栏可切换画质、全屏、粘贴剪贴板文本。", ""),
    ("", ""),
    ("无障碍说明", "h"),
    ("板端未接显示器/键鼠，因此这里运行的是板端渲染的虚拟桌面，", ""),
    ("而不是 Weston 合成器。SSH、VS Code Remote-SSH 依然照常可用。", ""),
    ("", ""),
    ("常用命令", "h"),
    ("  uname -a          # 内核", ""),
    ("  free -h           # 内存", ""),
    ("  df -h             # 存储", ""),
    ("  python3 -V        # Python", ""),
    ("  v4l2-ctl --list-devices   # 摄像头", ""),
]


class Window:
    def __init__(self, kind: str, title: str, x: int, y: int, w: int, h: int) -> None:
        self.kind = kind
        self.title = title
        self.x, self.y, self.w, self.h = x, y, w, h
        self.visible = True
        self.maximized = False
        self.prev_rect = None
        self.grid = None          # (cols, rows) for terminal
        self.scroll = 0
        self.path = "/root"       # files
        self.lines: list[str] = []  # viewer
        self.viewer_title = ""

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def title_hit(self, px: int, py: int) -> bool:
        return self.contains(px, py) and py < self.y + TITLE


class Desktop:
    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.width, self.height = width, height
        self.lock = threading.RLock()
        self.windows: list[Window] = []
        self.focus: Window | None = None
        self.stats = {
            "cpu": 0.0, "mem": 0.0, "mem_used": 0, "mem_total": 0,
            "temp": 0.0, "load": (0.0, 0.0, 0.0), "uptime": "",
            "disk_used": 0, "disk_total": 0, "ip": "", "host": "",
        }
        self.cpu_hist = deque([0.0] * 60, maxlen=60)
        self.dirty = True
        self.term = Terminal(90, 28)
        self.term.respond = self._term_respond
        self.on_terminal_input = None   # callable(bytes)
        self.on_terminal_resize = None  # callable(cols, rows)
        self.on_shell_restart = None
        self.shell_alive = True
        self.drag: tuple | None = None
        self.pressed: tuple | None = None
        self.last_click = (0.0, 0, 0)
        self.cursor = (self.width // 2, self.height // 2)
        self.show_cursor = False
        self.clients = 0
        self.fps = 0.0
        self.encode_ms = 0.0
        self.started = time.time()
        self._build_windows()

    # ------------------------------------------------------------- windows
    def _build_windows(self) -> None:
        self.term_win = Window("terminal", "root@RK3576-Tronlong: ~", 22, 54, 812, 640)
        self.monitor_win = Window("monitor", "系统性能", self.width - 422, 54, 400, 300)
        self.files_win = Window("files", "文件", self.width - 422, 370, 400, 324)
        self.help_win = Window("help", "帮助 · 快捷键", 300, 140, 680, 520)
        self.viewer_win = Window("viewer", "查看", 180, 110, 760, 540)

        self.windows = [self.term_win, self.monitor_win, self.files_win, self.help_win]
        self.focus = self.term_win
        self._layout_terminal(self.term_win)
        self.help_win.visible = False
        self.viewer_win.visible = False
        self.windows.append(self.viewer_win)
        self._raise(self.term_win)

    def _layout_terminal(self, win: Window) -> None:
        cols = max(20, (win.w - 2 - TERM_PAD * 2) // CELL_W)
        rows = max(6, (win.h - TITLE - 2 - TERM_PAD * 2) // CELL_H)
        win.grid = (cols, rows)
        self.term.resize(cols, rows)
        if self.on_terminal_resize:
            self.on_terminal_resize(cols, rows)

    def term_grid(self) -> tuple[int, int]:
        return self.term_win.grid or (self.term.cols, self.term.rows)

    def _raise(self, win: Window) -> None:
        if win in self.windows:
            self.windows.remove(win)
            self.windows.append(win)
        self.focus = win
        self.dirty = True

    def ensure_window(self, kind: str) -> Window:
        win = {
            "terminal": self.term_win,
            "monitor": self.monitor_win,
            "files": self.files_win,
            "help": self.help_win,
        }[kind]
        if not win.visible:
            win.visible = True
        self._raise(win)
        return win

    def _term_respond(self, data: bytes) -> None:
        if self.on_terminal_input:
            self.on_terminal_input(data)

    # --------------------------------------------------------------- stats
    def set_stats(self, **values) -> None:
        self.stats.update(values)
        self.cpu_hist.append(float(self.stats.get("cpu", 0.0)))
        self.dirty = True

    def set_title(self, title: str) -> None:
        if title and title != self.term_win.title:
            self.term_win.title = title[:80]
            self.dirty = True

    # -------------------------------------------------------------- input
    def pointer_move(self, x: int, y: int) -> None:
        self.cursor = (max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y)))
        if self.drag:
            win, mode, ox, oy = self.drag
            if mode == "move":
                nx = self.cursor[0] - ox
                ny = self.cursor[1] - oy
                win.x = max(-win.w + 120, min(self.width - 80, nx))
                win.y = max(TOPBAR + 2, min(self.height - DOCK - 40, ny))
            elif mode == "resize":
                win.w = max(340, min(self.width - win.x - 4, self.cursor[0] - win.x))
                win.h = max(220, min(self.height - DOCK - win.y - 4, self.cursor[1] - win.y))
                if win.kind == "terminal":
                    self._layout_terminal(win)
            self.dirty = True

    def pointer_down(self, x: int, y: int, button: int = 0) -> None:
        self.cursor = (x, y)
        now = time.monotonic()
        if button == 0:
            if now - self.last_click[0] < 0.42 and abs(x - self.last_click[1]) < 6 and abs(y - self.last_click[2]) < 6:
                win = self._hit(x, y)
                if win and win.title_hit(x, y) and not self._button_hit(win, x, y):
                    self._toggle_max(win)
                    self.last_click = (0.0, 0, 0)
                    return
            self.last_click = (now, x, y)

        if y >= self.height - DOCK:
            index = (x - 16) // 116
            if 0 <= index < len(DOCK_ITEMS) and 16 + index * 116 < x < 16 + index * 116 + 108:
                win = self.ensure_window(DOCK_ITEMS[index][0])
                if win is self.focus and win.visible:
                    pass
            return

        win = self._hit(x, y)
        if not win:
            self.focus = None
            self.dirty = True
            return

        action = self._button_hit(win, x, y)
        if action == "close":
            win.visible = False
            self.focus = None
            self.dirty = True
            return
        if action == "min":
            win.visible = False
            self.focus = None
            self.dirty = True
            return
        if action == "max":
            self._toggle_max(win)
            return

        self._raise(win)
        if win.kind == "terminal" and y < win.y + TITLE:
            win.scroll = 0
            self.term.offset = 0
        if win.kind == "files":
            self._files_click(win, x, y)
        elif win.kind == "viewer":
            pass

        if win.title_hit(x, y) and x < win.x + win.w - 18:
            self.drag = (win, "move", x - win.x, y - win.y)
        elif x > win.x + win.w - 18 and y > win.y + win.h - 18:
            self.drag = (win, "resize", 0, 0)
        self.pressed = (win, x, y, button)
        self.dirty = True

    def pointer_up(self, x: int, y: int, button: int = 0) -> None:
        if self.pressed and self.pressed[0].kind == "terminal":
            self.focus = self.pressed[0]
        self.drag = None
        self.pressed = None
        self.dirty = True

    def wheel(self, x: int, y: int, delta: float) -> None:
        win = self._hit(x, y)
        if not win:
            return
        if win.kind == "terminal":
            self.term.scroll(int(delta * 3))
        elif win.kind in ("files", "viewer"):
            win.scroll = max(0, win.scroll - int(delta * 3))
            self.dirty = True

    def _toggle_max(self, win: Window) -> None:
        if win.maximized:
            if win.prev_rect:
                win.x, win.y, win.w, win.h = win.prev_rect
            win.maximized = False
        else:
            win.prev_rect = (win.x, win.y, win.w, win.h)
            win.x, win.y = 8, TOPBAR + 6
            win.w, win.h = self.width - 16, self.height - DOCK - TOPBAR - 12
            win.maximized = True
        if win.kind == "terminal":
            self._layout_terminal(win)
        self._raise(win)

    def _hit(self, x: int, y: int) -> Window | None:
        for win in reversed(self.windows):
            if win.visible and win.contains(x, y):
                return win
        return None

    @staticmethod
    def _button_hit(win: Window, x: int, y: int) -> str | None:
        if not win.title_hit(x, y):
            return None
        cy = win.y + TITLE // 2
        for index, name in enumerate(("close", "min", "max")):
            bx = win.x + 16 + index * 18
            if (x - bx) ** 2 + (y - cy) ** 2 <= 49:
                return name
        return None

    def _files_click(self, win: Window, x: int, y: int) -> None:
        index = (y - win.y - TITLE - 30) // 26 + win.scroll
        entries = self._files_entries(win.path)
        if 0 <= index < len(entries):
            name, is_dir, _size = entries[index]
            if is_dir:
                win.path = os.path.abspath(os.path.join(win.path, name))
                win.scroll = 0
                self.dirty = True
            else:
                self._open_viewer(os.path.join(win.path, name))

    def _open_viewer(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
            if size > 256 * 1024:
                text = f"[文件过大] {size} bytes\n"
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(256 * 1024)
        except OSError as exc:
            text = f"[无法读取] {exc}\n"
        self.viewer_win.viewer_title = os.path.basename(path)
        self.viewer_win.lines = text.replace("\t", "    ").splitlines()[:2000] or [""]
        self.viewer_win.path = path
        self.viewer_win.visible = True
        self.viewer_win.scroll = 0
        self._raise(self.viewer_win)

    def _files_entries(self, path: str):
        entries = []
        try:
            parent = os.path.dirname(path.rstrip("/")) or "/"
            if path.rstrip("/") != "/":
                entries.append(("..", True, 0))
            items = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for item in items[:250]:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else item.stat(follow_symlinks=False).st_size
                except OSError:
                    is_dir, size = False, 0
                entries.append((item.name + ("/" if is_dir else ""), is_dir, size))
        except OSError as exc:
            entries.append((f"[无法读取] {exc}", False, 0))
        return entries

    def key(self, event: dict) -> None:
        key = str(event.get("key", ""))
        ctrl = bool(event.get("ctrl"))
        alt = bool(event.get("alt"))
        text = event.get("text")

        if key in ("F1", "F2", "F3", "F4") and not ctrl and not alt:
            target = {"F1": "help", "F2": "terminal", "F3": "monitor", "F4": "files"}[key]
            self.ensure_window(target)
            return

        win = self.focus
        if not win or win.kind != "terminal":
            return
        if not self.shell_alive:
            if key == "Enter" and self.on_shell_restart:
                self.on_shell_restart()
            return

        payload = b""
        if ctrl and len(key) == 1:
            char = key.lower()
            if "a" <= char <= "z":
                payload = bytes([ord(char) - 96])
            elif char == " ":
                payload = b"\x00"
            elif char == "[":
                payload = b"\x1b"
            elif char == "\\":
                payload = b"\x1c"
            elif char == "]":
                payload = b"\x1d"
        elif alt and text:
            payload = b"\x1b" + str(text).encode("utf-8")
        elif key in SPECIAL_KEYS:
            payload = SPECIAL_KEYS[key]
        elif text:
            payload = str(text).encode("utf-8")

        if payload:
            if self.term.offset:
                self.term.offset = 0
            if self.on_terminal_input:
                self.on_terminal_input(payload)
            self.term.dirty = True

    def paste(self, text: str) -> None:
        if self.focus and self.focus.kind == "terminal" and self.on_terminal_input:
            self.term.offset = 0
            self.on_terminal_input(text.encode("utf-8"))

    # -------------------------------------------------------------- render
    def render(self) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), BG_TOP)
        draw = ImageDraw.Draw(image)
        for y in range(self.height):
            draw.line([(0, y), (self.width, y)], fill=_mix(BG_TOP, BG_BOTTOM, y / self.height))
        for gx in range(0, self.width, 48):
            draw.line([(gx, TOPBAR), (gx, self.height - DOCK)], fill=GRID)
        for gy in range(TOPBAR, self.height - DOCK, 48):
            draw.line([(0, gy), (self.width, gy)], fill=GRID)

        for win in self.windows:
            if win.visible:
                self._draw_window(image, draw, win)
        self._draw_topbar(draw)
        self._draw_dock(draw)
        if self.show_cursor:
            self._draw_cursor(draw)
        return image

    def _draw_window(self, image: Image.Image, draw: ImageDraw.ImageDraw, win: Window) -> None:
        focused = win is self.focus
        x, y, w, h = win.x, win.y, win.w, win.h
        draw.rounded_rectangle([x + 5, y + 7, x + w + 5, y + h + 7], radius=RADIUS, fill=(4, 7, 10))
        draw.rounded_rectangle([x, y, x + w, y + h], radius=RADIUS, fill=PANEL,
                               outline=BORDER_FOCUS if focused else BORDER, width=1)
        draw.rounded_rectangle([x + 1, y + 1, x + w - 1, y + 8], radius=RADIUS,
                               fill=TITLE_BG if focused else TITLE_BG_IDLE)
        draw.rectangle([x + 1, y + 6, x + w - 1, y + TITLE], fill=TITLE_BG if focused else TITLE_BG_IDLE)
        for index, colour in enumerate((DANGER, WARN, ACCENT)):
            cx = x + 16 + index * 18
            cy = y + TITLE // 2
            draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=colour)
        title = win.viewer_title if win.kind == "viewer" and win.viewer_title else win.title
        draw.text((x + 62, y + 9), self._fit(draw, title, UI_BOLD, w - 100), font=UI_BOLD,
                  fill=TEXT if focused else TEXT_DIM)
        draw.line([(x + 1, y + TITLE), (x + w - 1, y + TITLE)], fill=BORDER)
        if win.kind == "terminal":
            self._draw_terminal(draw, win, focused)
        elif win.kind == "monitor":
            self._draw_monitor(draw, win)
        elif win.kind == "files":
            self._draw_files(draw, win)
        elif win.kind == "help":
            self._draw_help(draw, win)
        elif win.kind == "viewer":
            self._draw_viewer(draw, win)
        if not win.maximized:
            for offset in (0, 5, 10):
                draw.line([(x + w - 14 + offset, y + h - 4), (x + w - 4, y + h - 14 + offset)],
                          fill=TEXT_FAINT)

    def _draw_terminal(self, draw: ImageDraw.ImageDraw, win: Window, focused: bool) -> None:
        cols, rows = win.grid or (90, 28)
        ox = win.x + 1 + TERM_PAD
        oy = win.y + TITLE + 1 + TERM_PAD
        width = cols * CELL_W
        height = rows * CELL_H
        draw.rectangle([ox - TERM_PAD, oy - TERM_PAD, ox + width + TERM_PAD, oy + height + TERM_PAD], fill=DEFAULT_BG)
        rows_data = self.term.visible_rows()[:rows]
        for row_index, row in enumerate(rows_data):
            base_y = oy + row_index * CELL_H
            col = 0
            while col < min(cols, len(row)):
                cell = row[col]
                ch, fg, bg, bold, cw = cell
                if cw == 0:
                    col += 1
                    continue
                if cw == 2:
                    if bg != DEFAULT_BG:
                        draw.rectangle([ox + col * CELL_W, base_y, ox + (col + 2) * CELL_W - 1, base_y + CELL_H - 1], fill=bg)
                    if ch.strip():
                        draw.text((ox + col * CELL_W + 1, base_y - 1), ch, font=CJK, fill=fg)
                    col += 2
                    continue
                start = col
                parts = [ch]
                col += 1
                while col < min(cols, len(row)):
                    nxt = row[col]
                    if nxt[4] != 1 or (nxt[1], nxt[2], nxt[3]) != (fg, bg, bold):
                        break
                    parts.append(nxt[0])
                    col += 1
                text = "".join(parts)
                if bg != DEFAULT_BG:
                    draw.rectangle([ox + start * CELL_W, base_y,
                                    ox + col * CELL_W - 1, base_y + CELL_H - 1], fill=bg)
                if text.strip():
                    draw.text((ox + start * CELL_W, base_y - 1), text,
                              font=MONO_BOLD if bold else MONO, fill=fg)

        if focused and self.term.cursor_visible and self.term.offset == 0:
            cx = ox + self.term.cx * CELL_W
            cy = oy + self.term.cy * CELL_H
            draw.rectangle([cx, cy, cx + CELL_W - 1, cy + CELL_H - 1], fill=ACCENT)
            row = self.term.lines[self.term.cy]
            if self.term.cx < len(row) and row[self.term.cx][0].strip():
                draw.text((cx, cy - 1), row[self.term.cx][0], font=MONO, fill=DEFAULT_BG)
        if not self.shell_alive:
            draw.rectangle([ox, oy + height - CELL_H * 2, ox + width, oy + height], fill=(60, 20, 20))
            draw.text((ox + 6, oy + height - CELL_H * 2 + 3), "shell 已退出，按 Enter 重启", font=UI, fill=(255, 200, 200))
        if self.term.offset:
            label = f"回看 {self.term.offset} 行 · 滚轮回到底部"
            draw.rectangle([ox + width - 210, oy + height - 22, ox + width, oy + height], fill=(30, 45, 58))
            draw.text((ox + width - 202, oy + height - 19), label, font=SMALL, fill=TEXT_DIM)

    def _draw_monitor(self, draw: ImageDraw.ImageDraw, win: Window) -> None:
        s = self.stats
        x, y, w = win.x + 18, win.y + TITLE + 16, win.w - 36
        cpu = float(s.get("cpu", 0))
        mem = float(s.get("mem", 0))
        temp = float(s.get("temp", 0))
        draw.text((x, y), "CPU", font=SMALL, fill=TEXT_DIM)
        draw.text((x + w, y), f"{cpu:4.1f}%", font=SMALL_MONO, fill=ACCENT if cpu < 80 else WARN, anchor="ra")
        draw.rounded_rectangle([x, y + 16, x + w, y + 24], radius=4, fill=(28, 40, 51))
        draw.rounded_rectangle([x, y + 16, x + max(6, int(w * min(cpu, 100) / 100)), y + 24], radius=4, fill=ACCENT)
        y += 40

        draw.text((x, y), "内存", font=SMALL, fill=TEXT_DIM)
        draw.text((x + w, y), f"{s.get('mem_used', 0)} / {s.get('mem_total', 0)} MB", font=SMALL_MONO, fill=TEXT, anchor="ra")
        draw.rounded_rectangle([x, y + 16, x + w, y + 24], radius=4, fill=(28, 40, 51))
        draw.rounded_rectangle([x, y + 16, x + max(6, int(w * min(mem, 100) / 100)), y + 24], radius=4, fill=(70, 160, 240))
        y += 40

        draw.text((x, y), "温度", font=SMALL, fill=TEXT_DIM)
        draw.text((x + w, y), f"{temp:.1f} °C", font=SMALL_MONO, fill=WARN if temp > 70 else TEXT, anchor="ra")
        draw.rounded_rectangle([x, y + 16, x + w, y + 24], radius=4, fill=(28, 40, 51))
        draw.rounded_rectangle([x, y + 16, x + max(6, int(w * min(temp, 100) / 100)), y + 24], radius=4, fill=WARN)
        y += 38

        load = s.get("load", (0, 0, 0))
        draw.text((x, y), f"负载  {load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}", font=SMALL_MONO, fill=TEXT_DIM)
        draw.text((x, y + 22), f"运行  {s.get('uptime', '')}", font=SMALL, fill=TEXT_DIM)
        draw.text((x, y + 44), f"磁盘  {s.get('disk_used', 0)} / {s.get('disk_total', 0)} GB", font=SMALL, fill=TEXT_DIM)

        hist = list(self.cpu_hist)
        chart_y = win.y + win.h - 66
        chart_h = 46
        draw.line([(x, chart_y + chart_h), (x + w, chart_y + chart_h)], fill=BORDER)
        points = []
        for index, value in enumerate(hist):
            px = x + int(index * w / max(1, len(hist) - 1))
            py = chart_y + chart_h - int(min(value, 100) / 100 * chart_h)
            points.append((px, py))
        if len(points) > 1:
            draw.line(points, fill=ACCENT, width=1)
        draw.text((x, chart_y - 4), "CPU 60s", font=SMALL, fill=TEXT_FAINT)

    def _draw_files(self, draw: ImageDraw.ImageDraw, win: Window) -> None:
        x, y, w = win.x + 12, win.y + TITLE + 10, win.w - 24
        draw.rectangle([x, y, x + w, y + 22], fill=(12, 20, 27))
        draw.text((x + 6, y + 4), self._fit(draw, win.path, SMALL_MONO, w - 12), font=SMALL_MONO, fill=TEXT_DIM)
        top = y + 30
        row_h = 26
        visible = (win.h - TITLE - 50) // row_h
        entries = self._files_entries(win.path)
        win.scroll = max(0, min(win.scroll, max(0, len(entries) - visible)))
        for index in range(visible):
            item_index = index + win.scroll
            if item_index >= len(entries):
                break
            name, is_dir, size = entries[item_index]
            ry = top + index * row_h
            if index % 2 == 0:
                draw.rectangle([x, ry, x + w, ry + row_h - 2], fill=(20, 29, 38))
            if is_dir:
                draw.polygon([(x + 7, ry + 9), (x + 7, ry + 17), (x + 13, ry + 13)], fill=ACCENT)
            else:
                draw.rectangle([x + 8, ry + 9, x + 12, ry + 17], outline=TEXT_FAINT)
            label = self._fit(draw, name, SMALL_MONO, w - 90)
            draw.text((x + 20, ry + 5), label, font=SMALL_MONO, fill=TEXT if is_dir else (170, 186, 200))
            if not is_dir:
                draw.text((x + w - 6, ry + 5), self._size(size), font=SMALL_MONO, fill=TEXT_FAINT, anchor="ra")
        if len(entries) > visible:
            bar_x = x + w - 3
            draw.rectangle([bar_x, top, bar_x + 2, top + visible * row_h], fill=(30, 44, 57))
            pos = top + int(win.scroll / max(1, len(entries)) * visible * row_h)
            draw.rectangle([bar_x, pos, bar_x + 2, pos + max(10, int(visible / len(entries) * visible * row_h))], fill=ACCENT)

    def _draw_help(self, draw: ImageDraw.ImageDraw, win: Window) -> None:
        x, y = win.x + 22, win.y + TITLE + 16
        for text, style in HELP_LINES:
            if style == "title":
                draw.text((x, y), text, font=BIG, fill=ACCENT)
                y += 30
            elif style == "h":
                y += 6
                draw.text((x, y), text, font=UI_BOLD, fill=(150, 215, 195))
                y += 24
            else:
                draw.text((x, y), text, font=UI, fill=TEXT if text else TEXT_FAINT)
                y += 21

    def _draw_viewer(self, draw: ImageDraw.ImageDraw, win: Window) -> None:
        x, y, w = win.x + 14, win.y + TITLE + 10, win.w - 28
        line_h = 18
        visible = (win.h - TITLE - 20) // line_h
        win.scroll = max(0, min(win.scroll, max(0, len(win.lines) - visible)))
        for index in range(visible):
            line_index = index + win.scroll
            if line_index >= len(win.lines):
                break
            draw.text((x, y + index * line_h), self._fit(draw, win.lines[line_index], SMALL_MONO, w),
                      font=SMALL_MONO, fill=TEXT)

    def _draw_topbar(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle([0, 0, self.width, TOPBAR], fill=(13, 19, 26))
        draw.line([(0, TOPBAR), (self.width, TOPBAR)], fill=BORDER)
        draw.rounded_rectangle([12, 8, 32, 28], radius=6, fill=ACCENT_DARK, outline=ACCENT)
        draw.text((17, 10), "R", font=UI_BOLD, fill=ACCENT)
        draw.text((42, 8), "RK3576 · Remote Workbench", font=UI_BOLD, fill=TEXT)
        draw.text((42, 21), "Buildroot 2024.02 · Linux 6.1.115-rt · aarch64", font=SMALL, fill=TEXT_FAINT)
        clock = time.strftime("%H:%M:%S")
        draw.text((self.width - 16, 9), clock, font=SMALL_MONO, fill=TEXT, anchor="ra")
        info = f"{self.stats.get('ip', '')}   CPU {self.stats.get('cpu', 0):.0f}%   MEM {self.stats.get('mem', 0):.0f}%"
        draw.text((self.width - 16, 21), info, font=SMALL_MONO, fill=TEXT_DIM, anchor="ra")
        if self.clients:
            draw.ellipse([self.width - 322, 13, self.width - 314, 21], fill=ACCENT)
            draw.text((self.width - 308, 8), f"{self.clients} 客户端", font=SMALL, fill=TEXT_DIM)

    def _draw_dock(self, draw: ImageDraw.ImageDraw) -> None:
        top = self.height - DOCK
        draw.rectangle([0, top, self.width, self.height], fill=(14, 21, 28))
        draw.line([(0, top), (self.width, top)], fill=BORDER)
        for index, (kind, label, icon) in enumerate(DOCK_ITEMS):
            bx = 16 + index * 116
            by = top + 7
            active = bool(self.focus and self.focus.kind == kind and self.focus.visible)
            draw.rounded_rectangle([bx, by, bx + 108, by + 32], radius=7,
                                   fill=ACCENT_DARK if active else (22, 32, 41),
                                   outline=ACCENT if active else BORDER)
            draw.rounded_rectangle([bx + 7, by + 7, bx + 25, by + 25], radius=5,
                                   fill=ACCENT if active else (35, 49, 62))
            draw.text((bx + 12, by + 8), icon, font=SMALL_MONO, fill=(8, 15, 18) if active else TEXT_DIM)
            draw.text((bx + 34, by + 8), label, font=UI, fill=TEXT if active else TEXT_DIM)
        draw.text((self.width - 16, top + 16), f"{self.fps:.0f} fps · {self.encode_ms:.0f} ms/帧",
                  font=SMALL_MONO, fill=TEXT_FAINT, anchor="ra")

    def _draw_cursor(self, draw: ImageDraw.ImageDraw) -> None:
        x, y = self.cursor
        points = [(x, y), (x, y + 17), (x + 4, y + 13), (x + 7, y + 20),
                  (x + 10, y + 18), (x + 7, y + 11), (x + 12, y + 11)]
        draw.polygon(points, fill=(245, 250, 252), outline=(10, 15, 20))
        draw.line(points + [points[0]], fill=(10, 15, 20), width=1)

    @staticmethod
    def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
        if draw.textlength(text, font=font) <= max_width:
            return text
        ellipsis = "…"
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if draw.textlength(text[:mid] + ellipsis, font=font) <= max_width:
                low = mid
            else:
                high = mid - 1
        return text[:low] + ellipsis

    @staticmethod
    def _size(value: int) -> str:
        for unit in ("B", "K", "M", "G"):
            if value < 1024 or unit == "G":
                return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
            value /= 1024
        return ""


def default_desktop(width: int = WIDTH, height: int = HEIGHT) -> Desktop:
    return Desktop(width, height)
