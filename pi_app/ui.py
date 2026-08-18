"""Presentation layer for the checkpoint display.

Tkinter gives you flat grey boxes unless you take it somewhere, so this
module owns the visual system: tokens, type scale, bordered surfaces, pill
rows and the small amount of motion the screen actually needs.

Motion policy follows the same rule as the web console — animate only what
carries meaning. A gate display is watched, not operated, so the two things
that move are the ones a person is waiting on: the confirmation progress
while PPE is being verified, and the countdown before the gate clears.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

# -- tokens ---------------------------------------------------------------
BG = "#070b14"
PANEL = "#0b1120"
CARD = "#111c30"
LINE = "#1e2c47"

INK = "#ffffff"
MUTED = "#94a3b8"
FAINT = "#4a5a75"

AMBER = "#d97706"
AMBER_INK = "#0b1120"

OK = "#4ade80"
OK_DIM = "#86efac"
OK_BG = "#04240f"
OK_CARD = "#0a3a1c"
OK_LINE = "#15803d"

BAD = "#f87171"
BAD_DIM = "#fca5a5"
BAD_BG = "#2c0707"
BAD_CARD = "#4a1010"
BAD_LINE = "#b91c1c"

# A site alert isn't a PPE ruling on the person at the gate — amber, not
# BAD's red, so it reads as a different kind of event at a glance.
HAZ = "#fbbf24"
HAZ_DIM = "#fde68a"
HAZ_BG = "#3f2404"
HAZ_CARD = "#5c3a08"
HAZ_LINE = "#92400e"

FAMILY = "DejaVu Sans"


class Type:
    """Type scale, built once and reused.

    Sizes are recomputed for the panel width so the verdict can never be
    clipped — on a gate display the one word that must be unambiguous is
    exactly the one that overflows first.
    """

    def __init__(self):
        self.display = tkfont.Font(family=FAMILY, size=34, weight="bold")
        self.eyebrow = tkfont.Font(family=FAMILY, size=9, weight="bold")
        self.title = tkfont.Font(family=FAMILY, size=15, weight="bold")
        self.body = tkfont.Font(family=FAMILY, size=12)
        self.small = tkfont.Font(family=FAMILY, size=10)
        self.mono_lg = tkfont.Font(family=FAMILY, size=18, weight="bold")
        self.glyph = tkfont.Font(family=FAMILY, size=30, weight="bold")
        self.avatar = tkfont.Font(family=FAMILY, size=19, weight="bold")

    # Widest line the verdict row ever has to render.
    WIDEST = "Access Granted"
    GLYPH_GUTTER = 12

    def fit(self, usable_px: int) -> None:
        """Scale the display face to the widest verdict it must render.

        Measured, not estimated. An em-width guess was wrong for DejaVu Bold
        and clipped the final letters of "Access Granted" — on a gate display
        the one word that must be unambiguous. Tk can measure the real string,
        so ask it and step down until it genuinely fits.
        """
        size = 40
        while size > 16:
            self.display.configure(size=size)
            self.glyph.configure(size=max(int(size * 0.85), 15))
            needed = (self.display.measure(self.WIDEST)
                      + self.glyph.measure("✓") + self.GLYPH_GUTTER)
            if needed <= usable_px:
                break
            size -= 1
        self.display.configure(size=size)
        self.glyph.configure(size=max(int(size * 0.85), 15))
        self.title.configure(size=max(int(size * 0.44), 12))
        self.body.configure(size=max(int(size * 0.35), 10))
        self.small.configure(size=max(int(size * 0.29), 9))
        self.avatar.configure(size=max(int(size * 0.56), 14))


def surface(parent, bg=CARD, line=LINE, **kw) -> tk.Frame:
    """A bordered panel. Tk has no border-colour, so the highlight ring is
    the only way to get a hairline that reads as an edge rather than a
    bevel."""
    return tk.Frame(
        parent, bg=bg,
        highlightbackground=line, highlightcolor=line, highlightthickness=1,
        bd=0, **kw,
    )


class Meter(tk.Canvas):
    """A thin determinate bar.

    Used for the two waits the worker experiences: confirming PPE, and the
    gate clearing itself. Both are cases where a person is standing there
    wondering whether anything is happening.
    """

    def __init__(self, parent, bg=PANEL, height=4):
        super().__init__(parent, bg=bg, height=height, highlightthickness=0, bd=0)
        self._track = self.create_rectangle(0, 0, 0, height, width=0, fill=LINE)
        self._fill = self.create_rectangle(0, 0, 0, height, width=0, fill=AMBER)
        self._h = height
        self.bind("<Configure>", self._resize)

    def _resize(self, event) -> None:
        self.coords(self._track, 0, 0, event.width, self._h)
        self._redraw()

    def set(self, fraction: float, colour: str = AMBER) -> None:
        self._fraction = max(0.0, min(fraction, 1.0))
        self.itemconfigure(self._fill, fill=colour)
        self._redraw()

    def _redraw(self) -> None:
        width = self.winfo_width() or 1
        frac = getattr(self, "_fraction", 0.0)
        self.coords(self._fill, 0, 0, int(width * frac), self._h)

    def repaint(self, bg: str, track: str) -> None:
        self.configure(bg=bg)
        self.itemconfigure(self._track, fill=track)


class CheckRow(tk.Frame):
    """One PPE requirement, as a pill.

    Status is carried by icon *and* wording, never colour alone — a gate is
    exactly where a colour-blind worker must still understand the answer.
    """

    def __init__(self, parent, label: str, type_: Type):
        super().__init__(parent, bg=PANEL,
                         highlightbackground=LINE, highlightcolor=LINE,
                         highlightthickness=1, bd=0)
        self.type = type_
        self.mark = tk.Label(self, text="–", bg=PANEL, fg=FAINT,
                             font=type_.title, width=2)
        self.mark.pack(side="left", padx=(12, 8), pady=9)
        self.name = tk.Label(self, text=label, bg=PANEL, fg=INK,
                             font=type_.body, anchor="w")
        self.name.pack(side="left", fill="x", expand=True)
        self.status = tk.Label(self, text="WAITING", bg=PANEL, fg=FAINT,
                               font=type_.eyebrow)
        self.status.pack(side="right", padx=12)

    def set_state(self, state: str) -> None:
        palette = {
            "ok": (OK_CARD, OK_LINE, OK, "✓", "DETECTED"),
            "bad": (BAD_CARD, BAD_LINE, BAD, "✕", "MISSING"),
            "idle": (PANEL, LINE, FAINT, "–", "WAITING"),
            # Shown before anyone badges in: the gate is listing what this
            # site demands, not yet ruling on a person. Amber, because it's
            # an instruction to read on the way up — not a pass or a fail.
            "required": (PANEL, LINE, AMBER, "•", "REQUIRED"),
        }[state]
        bg, line, fg, glyph, word = palette
        self.configure(bg=bg, highlightbackground=line, highlightcolor=line)
        for widget in (self.mark, self.name, self.status):
            widget.configure(bg=bg)
        self.mark.configure(text=glyph, fg=fg)
        self.status.configure(text=word, fg=fg)
        self.name.configure(fg=INK if state != "idle" else MUTED)
