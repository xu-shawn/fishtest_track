"""SVG charts for the README, drawn by hand so the job needs no plotting library.

Every chart is rendered twice, once for GitHub's light theme and once for dark.
The four test types own categorical colors 1-4 on every chart; totals use a
neutral gray, and finished-test outcomes use green for "passed" against grays,
so a color never means two different things on the page. Opened on its own
(not inside the README), a chart shows native tooltips on its bars.
"""

from __future__ import annotations

import base64
import math
import statistics
import struct
import zlib
from collections.abc import Callable, Iterable
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from html import escape

from .fishtest import LLR_BINS, STANDARD_LLR_BOUND, TEST_TYPES, TYPE_COLUMNS, bounds_kind
from .store import CHECK_FIELDS, Check, History, parse_iso

_N = len(TEST_TYPES)
TYPE_INDEX = {t: i for i, t in enumerate(TEST_TYPES)}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
WIDTH = 880
PAD = 20


@dataclass(frozen=True)
class Theme:
    surface: str
    border: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    types: tuple[str, ...]  # one per TEST_TYPES entry, in order; validated for color blindness
    passed: str  # gainers passed
    simplified: str  # simplifications passed: fishtest's own blue for them
    rejected: str
    other: str
    heat: str  # sequential blue; heatmap cells fade from the surface to this color
    ranked: tuple[str, ...]  # 8 categorical colors for the top authors/contributors, by rank
    others: str  # fill for the "everyone else" band


THEMES = {
    "light": Theme(
        surface="#fcfcfb", border="#0b0b0b1a", ink="#0b0b0b", ink2="#52514e",
        muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
        types=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6", "#e34948"),
        passed="#0ca30c", simplified="#66ccff", rejected="#c3c2b7", other="#e1e0d9",
        heat="#184f95", others="#e1e0d9",
        ranked=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"),
    ),
    "dark": Theme(
        surface="#1a1a19", border="#ffffff1a", ink="#f0efec", ink2="#c3c2b7",
        muted="#898781", grid="#2c2c2a", axis="#383835",
        types=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9", "#e66767"),
        passed="#0ca30c", simplified="#66ccff", rejected="#52514e", other="#383835",
        heat="#9ec5f4", others="#383835",
        ranked=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"),
    ),
}
# Status colors: "passed" above uses the good-status green (always with its label),
# and outage bands use critical red, only for hours when the API was unreachable.
OUTAGE = "#d03b3b"


# ---------------------------------------------------------------- primitives

def _n(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _text(x, y, s, fill, size=12, weight=None, anchor=None) -> str:
    attrs = f'x="{_n(x)}" y="{_n(y)}" fill="{fill}" font-size="{size}"'
    if weight:
        attrs += f' font-weight="{weight}"'
    if anchor:
        attrs += f' text-anchor="{anchor}"'
    return f"<text {attrs}>{escape(s)}</text>"


def _line(x1, y1, x2, y2, stroke, width=1) -> str:
    return (
        f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
        f'stroke="{stroke}" stroke-width="{width}"/>'
    )


def _doc(width: float, height: float, theme: Theme, label: str, body: Iterable[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(width)}" height="{_n(height)}" '
        f'viewBox="0 0 {_n(width)} {_n(height)}" role="img" aria-label="{escape(label)}" '
        f'font-family="{FONT}">'
        f'<rect x="0.5" y="0.5" width="{_n(width - 1)}" height="{_n(height - 1)}" rx="8" '
        f'fill="{theme.surface}" stroke="{theme.border}"/>'
        + "".join(body)
        + "</svg>\n"
    )


def text_width(s: str, size: float, bold: bool = False) -> float:
    """Rough width of `s` in a system sans, good enough for layout decisions."""
    total = 0.0
    for ch in s:
        if ch in "iljtf.,:;'|!()[] ·":
            total += 0.3
        elif ch in "mwMW%@":
            total += 0.85
        elif ch.isupper():
            total += 0.66
        else:
            total += 0.56
    return total * size * (1.06 if bold else 1)


def _truncate(s: str, size: float, max_width: float, bold: bool = False) -> str:
    if text_width(s, size, bold) <= max_width:
        return s
    while s and text_width(s + "…", size, bold) > max_width:
        s = s[:-1]
    return s + "…"


def _swatch(x, y, color, size=10) -> str:
    return f'<rect x="{_n(x)}" y="{_n(y)}" width="{size}" height="{size}" rx="2" fill="{color}"/>'


def _legend(items: list[tuple[str, str]], x, y, theme: Theme) -> list[str]:
    """Swatch + label pairs on one line, starting at x; `y` is the text baseline."""
    out = []
    for color, label in items:
        out.append(_swatch(x, y - 9, color))
        out.append(_text(x + 16, y, label, theme.ink2, 12))
        x += 16 + text_width(label, 12) + 18
    return out


def _legend_width(items: list[tuple[str, str]]) -> float:
    return sum(16 + text_width(label, 12) + 18 for _, label in items) - 18 if items else 0


def _part_color(theme: Theme, k: int) -> str:
    """Color of bar segment k: a test type, or neutral for index _N ("Other")."""
    return theme.types[k] if k < _N else theme.muted


def _type_items(theme: Theme, present: Iterable[int],
                values: list[int] | None = None) -> list[tuple[str, str]]:
    """Legend entries for the test types shown in a chart, in their fixed order."""
    shown = set(present)
    items = [(theme.types[k], f"{TEST_TYPES[k]} {values[k]:,}" if values else TEST_TYPES[k])
             for k in range(_N) if k in shown]
    if _N in shown:
        items.append((theme.muted, "Other"))
    return items


def _header(theme: Theme, items: list[tuple[str, str]], summary: str) -> tuple[list[str], float]:
    """Legend on the left, bold summary on the right; the summary moves to its
    own line above the legend when the two would collide. Returns the parts and
    the baseline of the last header line."""
    y = PAD + 13
    out: list[str] = []
    if summary and items and (_legend_width(items) + text_width(summary, 13, bold=True) + 24
                              > WIDTH - 2 * PAD):
        out.append(_text(PAD, y, summary, theme.ink, 13, 600))
        y += 24
        out += _legend(items, PAD, y, theme)
    else:
        out += _legend(items, PAD, y, theme)
        if summary:
            out.append(_text(WIDTH - PAD, y, summary, theme.ink, 13, 600, "end"))
    return out, y


def _hit(x, y, w, h, tip: str) -> str:
    """An invisible area that shows `tip` when the chart is opened on its own."""
    return (
        f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" fill="#000" '
        f'fill-opacity="0"><title>{escape(tip)}</title></rect>'
    )


def _round_right(x, y, w, h, r=4) -> str:
    """Path data for a horizontal bar with a rounded right end and square left end."""
    r = min(r, w, h / 2)
    return (
        f"M{_n(x)},{_n(y)} H{_n(x + w - r)} A{_n(r)},{_n(r)} 0 0 1 {_n(x + w)},{_n(y + r)} "
        f"V{_n(y + h - r)} A{_n(r)},{_n(r)} 0 0 1 {_n(x + w - r)},{_n(y + h)} H{_n(x)} Z"
    )


def _round_top(x, y_bottom, w, h, r=4) -> str:
    """Path data for a column with rounded top corners and a square base."""
    r = min(r, w / 2, h)
    yt = y_bottom - h
    return (
        f"M{_n(x)},{_n(y_bottom)} V{_n(yt + r)} A{_n(r)},{_n(r)} 0 0 1 {_n(x + r)},{_n(yt)} "
        f"H{_n(x + w - r)} A{_n(r)},{_n(r)} 0 0 1 {_n(x + w)},{_n(yt + r)} V{_n(y_bottom)} Z"
    )


# ---------------------------------------------------------------- scales

def nice_ticks(vmax: float, integer: bool = False, max_intervals: int = 5) -> list[float]:
    """Round tick values from 0 to at least `vmax`, using the tightest round step."""
    if not vmax or vmax <= 0:
        vmax = 1
    mag = 10 ** math.floor(math.log10(vmax / max_intervals))
    multiples = (1, 2, 5, 10) if integer and mag < 10 else (1, 2, 2.5, 5, 10)
    for m in multiples:
        step = max(1, round(m * mag)) if integer else m * mag
        if math.ceil(vmax / step - 1e-9) <= max_intervals:
            break
    count = max(1, math.ceil(vmax / step - 1e-9))
    return [round(i * step, 10) for i in range(count + 1)]


def compact(v: float) -> str:
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}".removesuffix(".0") + suffix
    return f"{v:.1f}".removesuffix(".0")


def short(v: float) -> str:
    """Three significant figures with a K/M/G suffix: 827M, 1.23G."""
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            x = v / div
            decimals = max(0, 2 - math.floor(math.log10(abs(x))))
            return f"{x:.{decimals}f}{suffix}"
    return f"{v:,.0f}"


_TIME_STEPS = [timedelta(hours=h) for h in (1, 2, 3, 6, 12)] + [
    timedelta(days=d) for d in (1, 2, 3, 7, 14, 28)
]
_MONDAY = datetime(1970, 1, 5, tzinfo=UTC)


def time_ticks(t0: datetime, t1: datetime, max_ticks: int) -> list[tuple[datetime, str]]:
    span = t1 - t0
    step = next((s for s in _TIME_STEPS if span / s <= max_ticks), _TIME_STEPS[-1])
    t = _MONDAY + math.ceil((t0 - _MONDAY) / step) * step
    ticks = []
    while t <= t1:
        if step < timedelta(days=1) and (t.hour or t.minute):
            label = f"{t:%H}:00"
        else:
            label = f"{t:%b} {t.day}"
        ticks.append((t, label))
        t += step
    return ticks


# ---------------------------------------------------------------- time frames

# Consecutive checks further apart than this are not joined by a line.
MAX_GAP = timedelta(hours=2, minutes=30)


def _window(checks: list[Check], now: datetime, days: int) -> tuple[datetime, datetime]:
    t1 = max(now, checks[-1].time) if checks else now
    t0 = max(t1 - timedelta(days=days), checks[0].time if checks else t1)
    return min(t0, t1 - timedelta(hours=12)), t1  # keep a readable minimum span


def _segments(points, max_gap: timedelta = MAX_GAP):
    """Split (time, value) points into runs without failed checks or long gaps."""
    segment = []
    for t, v in points:
        if v is None or (segment and t - segment[-1][0] > max_gap):
            if segment:
                yield segment
            segment = []
        if v is not None:
            segment.append((t, v))
    if segment:
        yield segment


def _merge_spans(times: list[datetime], half: timedelta) -> list[tuple[datetime, datetime]]:
    spans: list[list[datetime]] = []
    for t in sorted(times):
        if spans and t - half <= spans[-1][1]:
            spans[-1][1] = t + half
        else:
            spans.append([t - half, t + half])
    return [(a, b) for a, b in spans]


@dataclass
class Frame:
    """Plot area with a time x-axis and a zero-based y-axis."""

    left: float
    right: float
    top: float
    bottom: float
    t0: datetime
    t1: datetime
    y_max: float

    def x(self, t: datetime) -> float:
        span = (self.t1 - self.t0).total_seconds() or 1
        return self.left + (t - self.t0).total_seconds() / span * (self.right - self.left)

    def y(self, v: float) -> float:
        return self.bottom - v / self.y_max * (self.bottom - self.top)


def _frame(theme: Theme, frame: Frame, ticks: list[float], tick_text: Callable[[float], str],
           clip_left: float, clip_right: float, outages: list[datetime]) -> list[str]:
    out = []
    for a, b in _merge_spans(outages, timedelta(minutes=30)):
        xa, xb = max(frame.left, frame.x(a)), min(frame.right, frame.x(b))
        if xb > xa:
            out.append(
                f'<rect x="{_n(xa)}" y="{_n(frame.top)}" width="{_n(xb - xa)}" '
                f'height="{_n(frame.bottom - frame.top)}" fill="{OUTAGE}" fill-opacity="0.2">'
                f"<title>fishtest API unreachable</title></rect>"
            )
    for tv in ticks:
        ty = frame.y(tv)
        out.append(_line(frame.left, ty, frame.right, ty, theme.axis if tv == 0 else theme.grid))
        out.append(_text(frame.left - 8, ty + 4, tick_text(tv), theme.muted, 11, anchor="end"))
    max_labels = max(2, int((frame.right - frame.left) / 48))
    for tt, label in time_ticks(frame.t0, frame.t1, max_labels):
        tx = frame.x(tt)
        half = text_width(label, 11) / 2
        if tx - half < clip_left or tx + half > clip_right:
            continue
        out.append(_line(tx, frame.bottom, tx, frame.bottom + 4, theme.axis))
        out.append(_text(tx, frame.bottom + 17, label, theme.muted, 11, anchor="middle"))
    return out


def _outages(checks: list[Check], t0: datetime) -> list[datetime]:
    return [c.time for c in checks if not c.has_data and c.time >= t0]


# ---------------------------------------------------------------- line panels

@dataclass
class Panel:
    title: str
    attr: str
    value_text: Callable[[float], str]  # formats the latest value in the header
    integer: bool = True
    tick_text: Callable[[float], str] = compact  # formats y-axis ticks


def _line_panel(theme: Theme, checks: list[Check], panel: Panel, x, y, w, h,
                t0: datetime, t1: datetime, max_gap: timedelta = MAX_GAP) -> list[str]:
    points = [(c.time, getattr(c, panel.attr)) for c in checks if c.time >= t0]
    values = [v for _, v in points if v is not None]
    color = theme.ink2  # totals stay neutral; hue is reserved for test types

    out = [_text(x, y + 13, panel.title, theme.ink, 13, 600)]
    if values:
        latest = next(v for _, v in reversed(points) if v is not None)
        out.append(_text(x + w, y + 13, panel.value_text(latest), theme.ink, 13, 600, "end"))

    ticks = nice_ticks(max(values, default=0), panel.integer)
    frame = Frame(x + 44, x + w - 8, y + 32, y + h - 22, t0, t1, ticks[-1])
    out += _frame(theme, frame, ticks, panel.tick_text, x, x + w, _outages(checks, t0))

    for seg in _segments(points, max_gap):
        if len(seg) == 1:
            out.append(f'<circle cx="{_n(frame.x(seg[0][0]))}" cy="{_n(frame.y(seg[0][1]))}" '
                       f'r="2.5" fill="{color}"/>')
            continue
        coords = " ".join(f"{_n(frame.x(t))},{_n(frame.y(v))}" for t, v in seg)
        x_first, x_last = _n(frame.x(seg[0][0])), _n(frame.x(seg[-1][0]))
        base = _n(frame.bottom)
        out.append(f'<path d="M{x_first},{base} L{coords} L{x_last},{base} Z" '
                   f'fill="{color}" fill-opacity="0.1"/>')
        out.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
    last = next(((t, v) for t, v in reversed(points) if v is not None), None)
    if last:
        out.append(f'<circle cx="{_n(frame.x(last[0]))}" cy="{_n(frame.y(last[1]))}" r="4" '
                   f'fill="{color}" stroke="{theme.surface}" stroke-width="2"/>')
    return out


def activity_chart(checks: list[Check], now: datetime, theme: Theme, days: int = 14,
                   max_gap: timedelta = MAX_GAP) -> str:
    def whole(v: float) -> str:
        return f"{v:,.0f}"

    panels = [
        Panel("Tests running", "tests_active", whole),
        Panel("Authors with running tests", "authors_active", whole),
        Panel("Workers", "workers", whole),
        Panel("Contributors with workers", "contributors", whole),
        Panel("Nodes per second", "mnps", lambda v: short(v * 1e6),
              tick_text=lambda v: compact(v * 1e6), integer=False),
        Panel("Games per minute", "games_per_min", whole),
        Panel("Pending hours (fishtest estimate)", "pending_hours", lambda v: f"{v:,.0f} h",
              integer=False),
        Panel("Tests waiting for workers", "tests_pending", whole),
    ]
    t0, t1 = _window(checks, now, days)
    gap_x, gap_y, panel_h = 32, 24, 184
    panel_w = (WIDTH - 2 * PAD - gap_x) / 2
    body: list[str] = []
    for i, panel in enumerate(panels):
        px = PAD + (i % 2) * (panel_w + gap_x)
        py = PAD + (i // 2) * (panel_h + gap_y)
        body += _line_panel(theme, checks, panel, px, py, panel_w, panel_h, t0, t1, max_gap)
    rows = math.ceil(len(panels) / 2)
    height = 2 * PAD + rows * panel_h + (rows - 1) * gap_y
    label = (f"Tests running and waiting, authors, workers, contributors, nodes per second, "
             f"games per minute and pending hours over the last {days} days")
    return _doc(WIDTH, height, theme, label, body)


# ---------------------------------------------------------------- cores by test type over time

_TYPE_ATTRS = tuple(TYPE_COLUMNS[t] for t in TEST_TYPES)


def types_chart(checks: list[Check], now: datetime, theme: Theme, days: int = 14,
                max_gap: timedelta = MAX_GAP) -> str:
    t0, t1 = _window(checks, now, days)
    points = [
        (c.time, [getattr(c, a) or 0 for a in _TYPE_ATTRS] if c.has_data else None)
        for c in checks if c.time >= t0
    ]
    latest = next((v for _, v in reversed(points) if v is not None), None)

    present = sorted({k for _, v in points if v for k in range(_N) if v[k]})
    body, header_y = _header(theme, _type_items(theme, present, latest),
                             f"{sum(latest):,} cores in total" if latest else "")

    ticks = nice_ticks(max((sum(v) for _, v in points if v), default=0), integer=True)
    frame = Frame(PAD + 44, WIDTH - PAD - 48, header_y + 24, header_y + 24 + 210, t0, t1, ticks[-1])
    body += _frame(theme, frame, ticks, compact, PAD, WIDTH - PAD, _outages(checks, t0))

    for seg in _segments(points, max_gap):
        xs = [frame.x(t) for t, _ in seg]
        cum = [[sum(v[: k + 1]) for k in range(_N)] for _, v in seg]
        if len(seg) == 1:  # a lone check: draw it as a thin stacked column
            for k in range(_N):
                lo, hi = frame.y(cum[0][k - 1] if k else 0), frame.y(cum[0][k])
                if lo - hi > 0:
                    body.append(f'<rect x="{_n(xs[0] - 2)}" y="{_n(hi)}" width="4" '
                                f'height="{_n(lo - hi)}" fill="{theme.types[k]}"/>')
            continue
        for k in range(_N):
            upper = [f"{_n(x)},{_n(frame.y(c[k]))}" for x, c in zip(xs, cum)]
            lower = [f"{_n(x)},{_n(frame.y(c[k - 1] if k else 0))}" for x, c in zip(xs, cum)]
            body.append(f'<path d="M{" L".join(upper)} L{" L".join(reversed(lower))} Z" '
                        f'fill="{theme.types[k]}" fill-opacity="0.28"/>')
        # Top edges, drawn from the top band down so a band with zero cores
        # never paints over the band beneath it.
        for k in reversed(range(_N)):
            upper = " ".join(f"{_n(x)},{_n(frame.y(c[k]))}" for x, c in zip(xs, cum))
            body.append(f'<polyline points="{upper}" fill="none" stroke="{theme.types[k]}" '
                        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    # Direct labels at the right edge for bands thick enough to hold one.
    if latest is not None and points[-1][1] is not None:
        cum = [sum(latest[: k + 1]) for k in range(_N)]
        for k in range(_N):
            lo, hi = frame.y(cum[k - 1] if k else 0), frame.y(cum[k])
            if lo - hi >= 14:
                body.append(_text(frame.right + 8, (lo + hi) / 2 + 4, TEST_TYPES[k], theme.ink2, 11))

    height = frame.bottom + 24 + PAD
    label = f"Cores by test type ({', '.join(TEST_TYPES)}) over the last {days} days"
    return _doc(WIDTH, height, theme, label, body)


# ---------------------------------------------------------------- per-entity bands over time

@dataclass
class Band:
    name: str  # label drawn inside the band where it is thickest
    fill: str
    values: dict[datetime, float]  # value at each snapshot time (missing = 0)
    tip: str = ""
    labelled: bool = True
    opacity: float = 1.0


MAX_LABEL_WIDTH = 160  # longest name + value shown in the label column; longer names are cut
LEADER = 43  # leader line plus color key, between the plot and a label
LABEL_STEP = 13  # vertical spacing between labels
MIN_LABELLED_THICKNESS = 8  # px; thinner bands at the latest time are left to the tooltip


def _spread(anchors: list[float], top: float, bottom: float, step: float) -> list[float]:
    """Label positions as close to `anchors` (sorted top to bottom) as possible, at
    least `step` apart and within [top, bottom]."""
    ys: list[float] = []
    for a in anchors:
        ys.append(max(a, top if not ys else ys[-1] + step))
    if ys and ys[-1] > bottom:  # pushed past the bottom: shift the stack back up
        ys[-1] = bottom
        for i in range(len(ys) - 2, -1, -1):
            ys[i] = min(ys[i], ys[i + 1] - step)
    return ys


def _band_chart(theme: Theme, times: list[datetime], bands: list[Band], t0: datetime,
                t1: datetime, max_gap: timedelta, legend: list[tuple[str, str]], summary: str,
                label: str, integer: bool = True, separator: float = 1,
                value_text: Callable[[float], str] = lambda v: f"{v:,.0f}",
                max_labels: int = 12) -> str:
    """Stacked bands, bottom first. The bands still present at the latest time are
    named in a column on the right, with a thin leader line to each band's end."""
    body, header_y = _header(theme, legend, summary)
    times = sorted(t for t in times if t >= t0)
    totals = [sum(b.values.get(t, 0) for b in bands) for t in times]
    ticks = nice_ticks(max(totals, default=0), integer)
    top, bottom = header_y + 24, header_y + 24 + 240

    def ypx(v: float) -> float:
        return bottom - v / ticks[-1] * (bottom - top)

    # Which bands get a label depends only on their height at the latest time, so
    # pick them first and size the label column to fit just those labels.
    last = times[-1] if times else None
    ends = []  # (value now, anchor y, band) for clearly visible bands at the latest time
    level = 0.0
    for band in bands:
        value = band.values.get(last, 0) if last else 0
        lo, hi = ypx(level), ypx(level + value)
        level += value
        if band.labelled and value > 0 and lo - hi >= MIN_LABELLED_THICKNESS:
            ends.append((value, (lo + hi) / 2, band))
    shown = sorted(sorted(ends, key=lambda e: -e[0])[:max_labels], key=lambda e: e[1])
    widths = [text_width(b.name, 11, bold=True) + text_width(f" {value_text(v)}", 11)
              for v, _, b in shown]
    column = LEADER + min(MAX_LABEL_WIDTH, max(widths)) + 4 if shown else 12
    frame = Frame(PAD + 44, WIDTH - PAD - column, top, bottom, t0, t1, ticks[-1])
    body += _frame(theme, frame, ticks, compact, PAD, frame.right + 20, [])

    # Runs of consecutive snapshots without long gaps.
    runs: list[list[datetime]] = []
    for t in times:
        if runs and t - runs[-1][-1] <= max_gap:
            runs[-1].append(t)
        else:
            runs.append([t])

    base = {t: 0.0 for t in times}
    for band in bands:
        lower = dict(base)
        for t in times:
            base[t] += band.values.get(t, 0)
        title = f"<title>{escape(band.tip or band.name)}</title>"
        fill = f'fill="{band.fill}"' + (f' fill-opacity="{band.opacity}"' if band.opacity < 1 else "")
        for run in runs:
            if all(band.values.get(t, 0) == 0 for t in run):
                continue
            if len(run) == 1:
                t = run[0]
                hi, lo = frame.y(base[t]), frame.y(lower[t])
                body.append(f'<rect x="{_n(frame.x(t) - 2)}" y="{_n(hi)}" width="4" '
                            f'height="{_n(max(0.5, lo - hi))}" {fill}>{title}</rect>')
                continue
            upper = [f"{_n(frame.x(t))},{_n(frame.y(base[t]))}" for t in run]
            under = [f"{_n(frame.x(t))},{_n(frame.y(lower[t]))}" for t in reversed(run)]
            body.append(f'<path d="M{" L".join(upper)} L{" L".join(under)} Z" {fill}>{title}</path>')
            if separator:
                body.append(f'<polyline points="{" ".join(upper)}" fill="none" '
                            f'stroke="{theme.surface}" stroke-width="{separator}" '
                            f'stroke-linejoin="round"/>')

    # The largest current bands get a label, top to bottom like the stack.
    ys = _spread([a for _, a, _ in shown], frame.top + 4, frame.bottom - 2, LABEL_STEP)
    x_edge = frame.x(last) if last else frame.right
    key_x = frame.right + LEADER - 13  # color key, then the name and current value
    text_x = key_x + 13
    room = WIDTH - PAD - text_x
    for (value, anchor, band), y in zip(shown, ys):
        number = f" {value_text(value)}"
        name = _truncate(band.name, 11, room - text_width(number, 11) - 2, bold=True)
        opacity = f' fill-opacity="{band.opacity}"' if band.opacity < 1 else ""
        body.append(f'<polyline points="{_n(x_edge - 3)},{_n(anchor)} {_n(frame.right + 10)},'
                    f'{_n(anchor)} {_n(key_x - 6)},{_n(y)} {_n(key_x - 3)},{_n(y)}" fill="none" '
                    f'stroke="{theme.muted}" stroke-width="1" stroke-linejoin="round"/>')
        # The leader starts at a dot on the band itself.
        body.append(f'<circle cx="{_n(x_edge - 3)}" cy="{_n(anchor)}" r="2.5" fill="{theme.ink}" '
                    f'stroke="{theme.surface}" stroke-width="1"/>')
        body.append(f'<rect x="{_n(key_x)}" y="{_n(y - 4)}" width="8" height="8" rx="2" '
                    f'fill="{band.fill}"{opacity}/>')
        body.append(f'<text x="{_n(text_x)}" y="{_n(y + 4)}" font-size="11">'
                    f'<tspan fill="{theme.ink}" font-weight="600">{escape(name)}</tspan>'
                    f'<tspan fill="{theme.muted}">{escape(number)}</tspan></text>')
    return _doc(WIDTH, frame.bottom + 24 + PAD, theme, label, body)


def _top_names(series: dict[str, dict[datetime, float]], limit: int) -> list[str]:
    """Names with the largest totals over the window, largest first."""
    totals = {name: sum(v.values()) for name, v in series.items()}
    return sorted(totals, key=lambda n: (-totals[n], n.lower()))[:limit]


def entity_trend_chart(series: dict[str, dict[datetime, float]], times: list[datetime],
                       now: datetime, theme: Theme, *, days: int, max_gap: timedelta, what: str,
                       unit: str, label: str, top: int = 8, integer: bool = True) -> str:
    """Bands for the top entities by total over the window, plus one band for everyone else."""
    t1 = max([now] + times)
    t0 = max(t1 - timedelta(days=days), min(times, default=t1))
    t0 = min(t0, t1 - timedelta(hours=12))
    window = {n: {t: v for t, v in vals.items() if t >= t0} for n, vals in series.items()}
    window = {n: v for n, v in window.items() if any(v.values())}
    names = _top_names(window, top)
    latest = max(times) if times else None

    def tip(name: str, values: dict[datetime, float]) -> str:
        now_value = values.get(latest, 0) if latest else 0
        peak = max(values.values(), default=0)
        return f"{name}: {now_value:,.0f} {unit} now, peak {peak:,.0f}"

    # Each top entity gets its own color (slot by rank, never reused); everyone else is gray.
    bands = [Band(n, theme.ranked[i], window[n], tip(n, window[n])) for i, n in enumerate(names)]
    rest = [n for n in window if n not in names]
    if rest:
        others: dict[datetime, float] = defaultdict(float)
        for n in rest:
            for t, v in window[n].items():
                others[t] += v
        bands.append(Band(f"{len(rest)} others", theme.others, dict(others),
                          tip(f"{len(rest)} other {what}", others)))
    summary = f"{len(window)} {what} in the window"
    value_text = (lambda v: f"{v:,.0f}") if integer else (lambda v: f"{v:,.1f}")
    return _band_chart(theme, times, bands, t0, t1, max_gap, [], summary, label, integer,
                       value_text=value_text)


# Lightness offsets (OKLab) for telling same-type neighbors apart. Four are enough for
# any flat map (four-color theorem); equal perceptual steps keep every hue even.
SHADES = (-0.045, -0.015, 0.015, 0.045)  # centered on the type color; 0.09 darkest to lightest


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def shade(hex_color: str, lightness: float) -> str:
    """`hex_color` with its OKLab lightness shifted by `lightness`, hue and chroma kept."""
    h = hex_color.lstrip("#")
    r, g, b = (_srgb_to_linear(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_ + lightness
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    l3 = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
    m3 = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
    s3 = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3
    rgb = (4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
           -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
           -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3)
    return "#" + "".join(f"{round(_linear_to_srgb(c) * 255):02x}" for c in rgb)


def _color_exactly(nodes: list[str], adjacent: dict[str, set[str]], colors: int,
                   budget: int) -> dict[str, int] | None:
    """A coloring of `nodes` in which no two adjacent nodes match, found by DSATUR with
    backtracking; None if there is none or the search needs more than `budget` steps."""
    color: dict[str, int] = {}
    steps = 0

    def solve() -> bool:
        nonlocal steps
        free = [n for n in nodes if n not in color]
        if not free:
            return True
        steps += 1
        if steps > budget:
            return False
        # Most constrained first: most distinct colors among neighbors, then most neighbors.
        node = max(free, key=lambda n: (len({color[m] for m in adjacent.get(n, ()) if m in color}),
                                        len(adjacent.get(n, ()))))
        used = {color[m] for m in adjacent.get(node, ()) if m in color}
        for c in range(colors):
            if c not in used:
                color[node] = c
                if solve():
                    return True
                del color[node]
                if steps > budget:
                    return False
        return False

    return dict(color) if solve() else None


def _fewest_clashes(nodes: list[str], touching: dict[str, dict[str, int]],
                    colors: int) -> dict[str, int]:
    """Fallback: greedy coloring, then a few passes moving nodes to the color that
    clashes least (weighted by how many hours two bands touch)."""
    def clash(node: str, c: int, color: dict[str, int]) -> int:
        return sum(n for other, n in touching[node].items() if color.get(other) == c)

    color: dict[str, int] = {}
    for node in nodes:
        color[node] = min(range(colors), key=lambda c: clash(node, c, color))
    for _ in range(10):
        changed = False
        for node in nodes:
            best = min(range(colors), key=lambda c: (clash(node, c, color), c != color[node]))
            if best != color[node]:
                color[node], changed = best, True
        if not changed:
            break
    return color


def neighbor_shades(order: list[str], kind: dict[str, int],
                    snapshots: list[set[str]], budget: int = 20_000) -> dict[str, int]:
    """A shade per band such that bands of the same kind that sit directly on top of
    each other never match. `order` is the stacking order (bottom first) and each
    snapshot is the set of bands present at one time.

    The bands form a flat map, so four shades always suffice (four-color theorem).
    Each group of touching bands is colored exactly with a bounded search; a group
    that needs more than `budget` steps falls back to a fast heuristic."""
    touching: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for present in snapshots:
        stack = [b for b in order if b in present]
        for lower, upper in zip(stack, stack[1:]):
            if kind[lower] == kind[upper]:
                touching[lower][upper] += 1  # hours the two bands touch
                touching[upper][lower] += 1
    adjacent = {b: set(n) for b, n in touching.items()}

    shade: dict[str, int] = {}
    for band in order:
        if band in shade:
            continue
        group, todo = {band}, [band]  # every band connected to this one by touching
        while todo:
            for other in adjacent.get(todo.pop(), ()):
                if other not in group:
                    group.add(other)
                    todo.append(other)
        members = [b for b in order if b in group]
        exact = _color_exactly(members, adjacent, len(SHADES), budget)
        shade.update(exact if exact is not None else _fewest_clashes(members, touching, len(SHADES)))
    return shade


def tests_trend_chart(hourly: list[dict], tests_info: dict[str, dict[str, str]], now: datetime,
                      theme: Theme, days: int = 14) -> str:
    """One band per test, colored by test type, oldest tests at the bottom."""
    times = [parse_iso(row["time"]) for row in hourly]
    t1 = max([now] + times)
    t0 = min(max(t1 - timedelta(days=days), min(times, default=t1)), t1 - timedelta(hours=12))
    series: dict[str, dict[datetime, float]] = defaultdict(dict)
    for row, t in zip(hourly, times):
        if t >= t0:
            for test_id, cores in row.get("tests", {}).items():
                series[test_id][t] = cores

    def start(test_id: str) -> str:
        info = tests_info.get(test_id, {})
        return info.get("started") or min(series[test_id]).strftime("%Y-%m-%dT%H:%M:%SZ")

    def order(test_id: str) -> tuple[int, str]:
        return TYPE_INDEX.get(tests_info.get(test_id, {}).get("type", ""), _N), start(test_id)

    stacking = sorted(series, key=order)
    kind = {test_id: order(test_id)[0] for test_id in stacking}
    snapshots = [{tid for tid, v in row.get("tests", {}).items() if v > 0}
                 for row, t in zip(hourly, times) if t >= t0]
    shades = neighbor_shades(stacking, kind, snapshots)

    bands, present = [], set()
    for test_id in stacking:
        info = tests_info.get(test_id, {})
        k = kind[test_id]
        present.add(k)
        name = f"{info.get('username', '?')} · {info.get('branch', test_id[:8])}"
        peak = max(series[test_id].values())
        tip = f"{name} ({info.get('type', 'unknown type')}): peak {peak:,} cores"
        bands.append(Band(name, shade(_part_color(theme, k), SHADES[shades[test_id]]),
                          series[test_id], tip))
    running = len(hourly[-1].get("tests", {})) if hourly else 0
    return _band_chart(theme, times, bands, t0, t1, MAX_GAP, _type_items(theme, present),
                       f"{len(series)} tests in the window · {running} running now",
                       f"Cores per test over the last {days} days, one band per test, "
                       "colored by test type", separator=0)


# ---------------------------------------------------------------- long-range daily averages

LONG_HISTORY = timedelta(days=14)  # show daily-average charts once history is longer than this
DAILY_GAP = timedelta(days=2, hours=12)
_TEXT_FIELDS = {"time", "status", "reason"}
_FLOAT_FIELDS = {f.name for f in fields(Check) if "float" in str(f.type)}


def has_long_history(checks: list[Check]) -> bool:
    good = [c for c in checks if c.has_data]
    return len(good) > 1 and good[-1].time - good[0].time > LONG_HISTORY


def daily_means(checks: list[Check]) -> list[Check]:
    """One averaged check per UTC day (placed at noon), from the checks that have data."""
    by_day: dict[date, list[Check]] = defaultdict(list)
    for c in checks:
        if c.has_data:
            by_day[c.time.date()].append(c)
    days = []
    for day, group in sorted(by_day.items()):
        values = {}
        for name in CHECK_FIELDS:
            if name in _TEXT_FIELDS:
                continue
            present = [getattr(c, name) for c in group if getattr(c, name) is not None]
            if not present:
                values[name] = None
            elif name in _FLOAT_FIELDS:
                values[name] = round(sum(present) / len(present), 1)
            else:
                values[name] = round(sum(present) / len(present))
        days.append(Check(time=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
                          status="up", **values))
    return days


# ---------------------------------------------------------------- LLR distribution over time

def _png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """Encode RGBA rows as a PNG with nothing but zlib."""
    height, width = len(pixels), len(pixels[0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _rgba(hex_color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(255 * alpha)


LLR_SMOOTHING_HOURS = 2  # each column averages the shares of the hours within this distance


def llr_chart(rows: list[dict[str, str]], now: datetime, theme: Theme, days: int = 14) -> str:
    """Heatmap: one column per hour, one row per LLR bin, darker = larger share of tests.

    With ~50 tests over 16 bins each cell holds only a few tests, so each column
    shows the average share over a 5-hour window centered on it. The median
    line is not smoothed.
    """
    snapshots = []
    for r in rows:
        counts = [int(c) for c in r["counts"].split()]
        if int(r["tests"] or 0) > 0 and len(counts) == LLR_BINS:
            median = float(r["median"]) if r["median"] else None
            snapshots.append((parse_iso(r["time"]), int(r["tests"]), median, counts))
    snapshots.sort(key=lambda s: s[0])

    hour = timedelta(hours=1)
    end = (max(now, snapshots[-1][0]) if snapshots else now).replace(minute=0, second=0) + hour
    start = end - timedelta(days=days)
    if snapshots:
        first = snapshots[0][0].replace(minute=0, second=0)
        start = max(start, min(first, end - 12 * hour))
    slots = round((end - start) / hour)
    by_slot = {}
    for t, tests, median, counts in snapshots:
        i = int((t - start) / hour)
        if 0 <= i < slots:
            by_slot[i] = (t, tests, median, counts)  # the latest snapshot in each hour wins

    latest = snapshots[-1] if snapshots else None
    header_y = PAD + 13
    body: list[str] = []
    x = PAD
    body.append(_text(x, header_y, "Share of tests", theme.ink2, 12))
    x += text_width("Share of tests", 12) + 8
    for alpha in (0.2, 0.4, 0.6, 0.8, 1.0):
        body.append(f'<rect x="{_n(x)}" y="{_n(header_y - 9)}" width="10" height="10" rx="2" '
                    f'fill="{theme.heat}" fill-opacity="{alpha}"/>')
        x += 12
    x += 18
    body.append(f'<line x1="{_n(x)}" y1="{_n(header_y - 4)}" x2="{_n(x + 14)}" y2="{_n(header_y - 4)}" '
                f'stroke="{theme.ink}" stroke-width="2" stroke-linecap="round"/>')
    body.append(_text(x + 20, header_y, "Median", theme.ink2, 12))
    if latest:
        summary = f"{latest[1]} SPRT tests running · median LLR {latest[2]:+.2f}"
        body.append(_text(WIDTH - PAD, header_y, summary, theme.ink, 13, 600, "end"))

    frame = Frame(PAD + 44, WIDTH - PAD - 56, header_y + 24, header_y + 24 + 200, start, end, 1)
    bound = STANDARD_LLR_BOUND

    def ly(v: float) -> float:
        return frame.top + (bound - v) / (2 * bound) * (frame.bottom - frame.top)

    if by_slot:
        shares = {i: [c / tests for c in counts] for i, (_, tests, _, counts) in by_slot.items()}
        smooth = {}
        for i in shares:
            near = [shares[j] for j in range(i - LLR_SMOOTHING_HOURS, i + LLR_SMOOTHING_HOURS + 1)
                    if j in shares]
            smooth[i] = [sum(col[k] for col in near) / len(near) for k in range(LLR_BINS)]
        peak = max(v for col in smooth.values() for v in col) or 1
        clear = (0, 0, 0, 0)
        pixels = [[clear] * slots for _ in range(LLR_BINS)]
        for i, col in smooth.items():
            for k, v in enumerate(col):
                # One hue fading into the surface: sparse cells recede, dense ones stand out.
                pixels[LLR_BINS - 1 - k][i] = _rgba(theme.heat, v / peak)  # top row = pass side
        data = base64.b64encode(_png(pixels)).decode()
        body.append(
            f'<image x="{_n(frame.left)}" y="{_n(frame.top)}" width="{_n(frame.right - frame.left)}" '
            f'height="{_n(frame.bottom - frame.top)}" preserveAspectRatio="none" '
            f'image-rendering="optimizeSpeed" style="image-rendering:pixelated" '
            f'href="data:image/png;base64,{data}"/>'
        )

    for v, label in ((bound, "+2.94"), (bound / 2, "+1.47"), (0, "0"), (-bound / 2, "−1.47"),
                     (-bound, "−2.94")):
        body.append(_line(frame.left - 4, ly(v), frame.left, ly(v), theme.axis))
        body.append(_text(frame.left - 8, ly(v) + 4, label, theme.muted, 11, anchor="end"))
    body.append(_line(frame.left, frame.top, frame.left, frame.bottom, theme.axis))
    body.append(_line(frame.left, frame.bottom, frame.right, frame.bottom, theme.axis))
    body.append(_text(frame.right + 8, ly(bound) + 4, "pass", theme.ink2, 11))
    body.append(_text(frame.right + 8, ly(-bound) + 4, "reject", theme.ink2, 11))
    for tt, label in time_ticks(start, end, max(2, int((frame.right - frame.left) / 48))):
        tx = frame.x(tt)
        if PAD < tx - text_width(label, 11) / 2 and tx + text_width(label, 11) / 2 < WIDTH - PAD:
            body.append(_line(tx, frame.bottom, tx, frame.bottom + 4, theme.axis))
            body.append(_text(tx, frame.bottom + 17, label, theme.muted, 11, anchor="middle"))

    # Median: an ink line with a surface-colored halo so it reads over any cell.
    slot_w = (frame.right - frame.left) / slots
    segment: list[str] = []
    segments = []
    for i in range(slots):
        snap = by_slot.get(i)
        if snap and snap[2] is not None:
            segment.append(f"{_n(frame.left + (i + 0.5) * slot_w)},{_n(ly(snap[2]))}")
        elif segment:
            segments.append(segment)
            segment = []
    if segment:
        segments.append(segment)
    for seg in segments:
        if len(seg) == 1:
            cx, cy = seg[0].split(",")
            body.append(f'<circle cx="{cx}" cy="{cy}" r="3" fill="{theme.ink}" '
                        f'stroke="{theme.surface}" stroke-width="1.5"/>')
            continue
        points = " ".join(seg)
        body.append(f'<polyline points="{points}" fill="none" stroke="{theme.surface}" '
                    f'stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>')
        body.append(f'<polyline points="{points}" fill="none" stroke="{theme.ink}" '
                    f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>')

    for i, (t, tests, median, _) in sorted(by_slot.items()):
        tip = f"{t:%b} {t.day}, {t:%H}:00 UTC — {tests} SPRT tests"
        if median is not None:
            tip += f", median LLR {median:+.2f}"
        body.append(_hit(frame.left + i * slot_w, frame.top, slot_w, frame.bottom - frame.top, tip))

    label = f"Distribution of the LLRs of running SPRT tests over the last {days} days"
    return _doc(WIDTH, frame.bottom + 24 + PAD, theme, label, body)


# ---------------------------------------------------------------- ranked bars (now)


@dataclass
class BarRow:
    name: str
    detail: str
    parts: list[int]  # cores per test type
    value: str
    note: str
    tip: str


def _bar_chart(theme: Theme, rows: list[BarRow], present: Iterable[int], header_right: str,
               footer: str, label: str, empty: str) -> str:
    label_w, value_w, row_h, bar_h = 260, 120, 24, 14
    body, header_y = _header(theme, _type_items(theme, present), header_right)

    top = header_y + 20
    x0, x1 = PAD + label_w, WIDTH - PAD - value_w
    if not rows:
        body.append(_text(PAD, top + 20, empty, theme.ink2, 13))
        return _doc(WIDTH, top + 32 + PAD, theme, label, body)

    v_max = max(sum(r.parts) for r in rows) or 1
    scale = (x1 - x0) / v_max
    for i, row in enumerate(rows):
        y = top + i * row_h
        cy = y + row_h / 2
        # "name · detail", right-aligned against the bars; detail is cut first.
        room = label_w - 12
        name = _truncate(row.name, 12, room * 0.6, bold=True)
        name_w = text_width(name, 12, bold=True)
        detail = _truncate(f" · {row.detail}", 12, room - name_w) if row.detail else ""
        body.append(
            f'<text x="{_n(x0 - 10)}" y="{_n(cy + 4)}" font-size="12" text-anchor="end">'
            f'<tspan fill="{theme.ink}" font-weight="600">{escape(name)}</tspan>'
            f'<tspan fill="{theme.ink2}">{escape(detail)}</tspan></text>'
        )
        x = x0
        nonzero = [k for k in range(len(row.parts)) if row.parts[k]]
        for k in nonzero:
            w = row.parts[k] * scale
            color = _part_color(theme, k)
            if k == nonzero[-1]:
                body.append(f'<path d="{_round_right(x, cy - bar_h / 2, w, bar_h)}" fill="{color}"/>')
            else:
                body.append(f'<rect x="{_n(x)}" y="{_n(cy - bar_h / 2)}" width="{_n(max(1, w - 2))}" '
                            f'height="{bar_h}" fill="{color}"/>')  # 2px gap after
            x += w
        body.append(
            f'<text x="{_n(x + 8)}" y="{_n(cy + 4)}" font-size="12">'
            f'<tspan fill="{theme.ink}">{escape(row.value)}</tspan>'
            f'<tspan fill="{theme.muted}">{escape(row.note)}</tspan></text>'
        )
        body.append(_hit(PAD, y, WIDTH - 2 * PAD, row_h, row.tip))
    bottom = top + len(rows) * row_h
    body.append(_line(x0, top, x0, bottom, theme.axis))
    if footer:
        body.append(_text(x0, bottom + 20, footer, theme.ink2, 12))
        bottom += 24
    return _doc(WIDTH, bottom + PAD, theme, label, body)


def _int(row: dict[str, str], key: str) -> int:
    return int(row[key]) if row.get(key) else 0


def tests_chart(active: list[dict[str, str]], theme: Theme, top_n: int = 15) -> str:
    running = sorted((r for r in active if _int(r, "cores") > 0), key=lambda r: -_int(r, "cores"))
    rows = []
    for r in running[:top_n]:
        parts = [0] * _N
        parts[TYPE_INDEX[r["type"]]] = _int(r, "cores")
        tip = (f"{r['username']} · {r['branch']} — {r['type']} {r['tc']}"
               f"{', ' + r['threads'] + ' threads' if r['threads'] != '1' else ''} — "
               f"{_int(r, 'cores'):,} cores on {_int(r, 'workers')} workers — "
               f"{_int(r, 'games'):,} games")
        if r.get("llr"):
            tip += f" — LLR {r['llr']}"
        rows.append(BarRow(r["username"], r["branch"], parts, f"{_int(r, 'cores'):,}",
                           f"  {r['type']}", tip))

    median = statistics.median(_int(r, "cores") for r in running) if running else 0
    rest = running[top_n:]
    footer = (f"+ {len(rest)} more {'test' if len(rest) == 1 else 'tests'} using "
              f"{sum(_int(r, 'cores') for r in rest):,} cores") if rest else ""
    present = {TYPE_INDEX[r["type"]] for r in running}
    return _bar_chart(
        theme, rows, present, f"{len(running)} tests running · median {median:,.0f} cores", footer,
        f"Cores per running test, top {min(top_n, len(running))} of {len(running)}",
        "No tests are running right now.",
    )


def authors_chart(active: list[dict[str, str]], theme: Theme, top_n: int = 15) -> str:
    by_author: dict[str, dict] = {}
    for r in active:
        cores = _int(r, "cores")
        if cores <= 0:
            continue
        a = by_author.setdefault(r["username"], {"parts": [0] * _N, "tests": 0})
        a["parts"][TYPE_INDEX[r["type"]]] += cores
        a["tests"] += 1
    ranked = sorted(by_author.items(), key=lambda kv: (-sum(kv[1]["parts"]), kv[0].lower()))
    total = sum(sum(a["parts"]) for _, a in ranked)

    rows = []
    for name, a in ranked[:top_n]:
        cores = sum(a["parts"])
        split = ", ".join(f"{a['parts'][k]:,} {TEST_TYPES[k]}" for k in range(_N) if a["parts"][k])
        tests = f"{a['tests']} {'test' if a['tests'] == 1 else 'tests'}"
        rows.append(BarRow(name, "", a["parts"], f"{cores:,}", f"  {tests}",
                           f"{name} — {cores:,} cores on {tests}: {split}"))

    top3 = sum(sum(a["parts"]) for _, a in ranked[:3])
    header = f"{len(ranked)} authors · top 3 use {100 * top3 / total:.0f}% of cores" if total else ""
    rest = ranked[top_n:]
    footer = (f"+ {len(rest)} more {'author' if len(rest) == 1 else 'authors'} using "
              f"{sum(sum(a['parts']) for _, a in rest):,} cores") if rest else ""
    present = {k for _, a in ranked for k in range(_N) if a["parts"][k]}
    return _bar_chart(theme, rows, present, header, footer,
                      f"Cores per author, top {min(top_n, len(ranked))} of {len(ranked)}",
                      "No tests are running right now.")


def contributors_chart(workers: list[dict[str, str]], active: list[dict[str, str]], theme: Theme,
                       top_n: int = 15) -> str:
    """Cores each user's workers contribute, split by the type of test they are playing."""
    run_type = {r["id"]: TYPE_INDEX[r["type"]] for r in active if r.get("type") in TYPE_INDEX}
    by_user: dict[str, dict] = {}
    for w in workers:
        u = by_user.setdefault(w["username"], {"parts": [0] * (_N + 1), "workers": 0})
        u["parts"][run_type.get(w["run"], _N)] += _int(w, "cores")
        u["workers"] += 1
    ranked = sorted(by_user.items(), key=lambda kv: (-sum(kv[1]["parts"]), kv[0].lower()))
    total = sum(sum(u["parts"]) for _, u in ranked)

    def label(k: int) -> str:
        return TEST_TYPES[k] if k < _N else "other"

    rows = []
    for name, u in ranked[:top_n]:
        cores = sum(u["parts"])
        count = f"{u['workers']} {'worker' if u['workers'] == 1 else 'workers'}"
        split = ", ".join(f"{u['parts'][k]:,} {label(k)}" for k in range(_N + 1) if u["parts"][k])
        rows.append(BarRow(name, "", u["parts"], f"{cores:,}", f"  {count}",
                           f"{name} — {cores:,} cores on {count}, playing {split}"))

    top3 = sum(sum(u["parts"]) for _, u in ranked[:3])
    header = (f"{len(ranked)} contributors · top 3 give {100 * top3 / total:.0f}% of cores"
              if total else "")
    rest = ranked[top_n:]
    footer = (f"+ {len(rest)} more {'contributor' if len(rest) == 1 else 'contributors'} with "
              f"{sum(sum(u['parts']) for _, u in rest):,} cores") if rest else ""
    present = {k for _, u in ranked for k in range(_N + 1) if u["parts"][k]}
    return _bar_chart(theme, rows, present, header, footer,
                      f"Cores contributed per user, top {min(top_n, len(ranked))} of {len(ranked)}",
                      "No workers are connected right now.")


# ---------------------------------------------------------------- finished tests per day

def _axis_labels(theme: Theme, labels: list[tuple[float, str]], y: float, right: float) -> list[str]:
    """Centered x-axis labels, placed right to left so the newest always shows: the last
    one is right-aligned if it would stick out, and any label that would overlap is skipped."""
    out: list[str] = []
    limit = right
    for cx, label in sorted(labels, reverse=True):
        width = text_width(label, 11)
        anchor_end = cx + width / 2 > right
        left_edge = right - width if anchor_end else cx - width / 2
        if (right if anchor_end else cx + width / 2) > limit:
            continue
        if anchor_end:
            out.append(_text(right, y, label, theme.muted, 11, anchor="end"))
        else:
            out.append(_text(cx, y, label, theme.muted, 11, anchor="middle"))
        limit = left_edge - 8
    return out


@dataclass
class Column:
    label: str  # x-axis label, or "" for none
    parts: list[float]  # stacked values, bottom first
    tip: str
    partial: bool = False  # still filling up (today / this week)


def _columns(theme: Theme, colors: list[str], columns: list[Column], left, right, top, bottom,
             integer: bool = True) -> list[str]:
    out: list[str] = []
    ticks = nice_ticks(max((sum(c.parts) for c in columns), default=0), integer)

    def sy(v: float) -> float:
        return bottom - v / ticks[-1] * (bottom - top)

    for tv in ticks:
        out.append(_line(left, sy(tv), right, sy(tv), theme.axis if tv == 0 else theme.grid))
        out.append(_text(left - 8, sy(tv) + 4, compact(tv), theme.muted, 11, anchor="end"))

    slot = (right - left) / max(1, len(columns))
    bar_w = min(24, slot * 0.7)
    out += _axis_labels(theme, [(left + (i + 0.5) * slot, c.label) for i, c in enumerate(columns)
                                if c.label], bottom + 17, right)
    for i, col in enumerate(columns):
        cx = left + (i + 0.5) * slot
        x = cx - bar_w / 2
        opacity = ' fill-opacity="0.55"' if col.partial else ""
        nonzero = [k for k in range(len(col.parts)) if col.parts[k] > 0]
        base = 0.0
        for k in nonzero:
            y_bottom = sy(base)
            base += col.parts[k]
            h = y_bottom - sy(base)
            if k != nonzero[-1]:
                h = max(1, h - 2)  # surface gap between stacked segments
                out.append(f'<rect x="{_n(x)}" y="{_n(y_bottom - h)}" width="{_n(bar_w)}" '
                           f'height="{_n(h)}" fill="{colors[k]}"{opacity}/>')
            else:
                out.append(f'<path d="{_round_top(x, y_bottom, bar_w, h, min(4, bar_w / 3))}" '
                           f'fill="{colors[k]}"{opacity}/>')
        out.append(_hit(left + i * slot, top, slot, bottom - top, col.tip))
    return out


GROUP_LABELS = ("Gainers passed", "Simplifications passed", "Rejected", "Other")


def _outcome_colors(theme: Theme) -> list[str]:
    return [theme.passed, theme.simplified, theme.rejected, theme.other]


def _outcome_group(r: dict[str, str]) -> int:
    if r["outcome"] == "passed":
        return 1 if bounds_kind(r.get("elo0"), r.get("elo1")) == "simplification" else 0
    return 2 if r["outcome"] == "rejected" else 3


def _outcome_counts(runs: Iterable[dict[str, str]]) -> list[int]:
    counts = [0, 0, 0, 0]
    for r in runs:
        counts[_outcome_group(r)] += 1
    return counts


def _outcome_legend(theme: Theme, totals: list[int]) -> list[tuple[str, str]]:
    """Legend entries for the outcome groups that occur (all of them if none do)."""
    colors = _outcome_colors(theme)
    shown = [k for k in range(4) if totals[k]] or list(range(4))
    return [(colors[k], f"{GROUP_LABELS[k]} {totals[k]:,}") for k in shown]


def _outcome_tip(counts: list[int]) -> str:
    return ", ".join(f"{counts[k]} {GROUP_LABELS[k].lower()}" for k in range(4) if counts[k])


def finished_chart(finished: dict[str, dict[str, str]], now: datetime, theme: Theme,
                   days: int = 30) -> str:
    today = now.date()
    first_day = today - timedelta(days=days - 1)
    if finished:
        earliest = min(parse_iso(r["finished"]).date() for r in finished.values())
        first_day = max(first_day, earliest)  # history may not reach back the full window
    by_day: dict[date, list] = defaultdict(list)
    for r in finished.values():
        by_day[parse_iso(r["finished"]).date()].append(r)
    day_list = [first_day + timedelta(days=i) for i in range((today - first_day).days + 1)]
    counts = [_outcome_counts(by_day[d]) for d in day_list]
    totals = [sum(c[i] for c in counts) for i in range(4)]

    body, header_y = _header(theme, _outcome_legend(theme, totals),
                             f"{sum(totals):,} tests in {len(day_list)} days")
    columns = []
    for i, (day, c) in enumerate(zip(day_list, counts)):
        partial = day == today
        label = ("Today" if partial else f"{day:%b} {day.day}") if (len(day_list) - 1 - i) % 5 == 0 else ""
        tip = (f"{day:%b} {day.day}{' (so far)' if partial else ''}: {sum(c)} tests finished"
               + (f" — {_outcome_tip(c)}" if sum(c) else ""))
        columns.append(Column(label, c, tip, partial))
    top = header_y + 24
    body += _columns(theme, _outcome_colors(theme), columns, PAD + 36, WIDTH - PAD, top, top + 180)

    label = f"Tests finished per day over {len(day_list)} days: {_outcome_tip(totals)}"
    return _doc(WIDTH, top + 180 + 24 + PAD, theme, label, body)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _week_list(finished: dict[str, dict[str, str]], now: datetime, weeks: int) -> list[date]:
    this_week = _week_start(now.date())
    first = this_week - timedelta(weeks=weeks - 1)
    if finished:
        earliest = min(parse_iso(r["finished"]) for r in finished.values())
        start = _week_start(earliest.date())
        if earliest > datetime(start.year, start.month, start.day, tzinfo=UTC):
            start += timedelta(weeks=1)  # the week the history starts in is incomplete
        first = max(first, min(start, this_week))
    return [first + timedelta(weeks=i) for i in range((this_week - first).days // 7 + 1)]


def _week_label(weeks: list[date], i: int) -> str:
    every = max(1, -(-len(weeks) // 13))  # about 13 labels at most: every 4th week over a year
    if (len(weeks) - 1 - i) % every:
        return ""
    return "This week" if i == len(weeks) - 1 else f"{weeks[i]:%b} {weeks[i].day}"


WEEKLY_KINDS = {"gainer": "Gainer bounds", "simplification": "Simplification bounds",
                "net": "Net tests", "search": "Search tests"}


def matches_kind(r: dict[str, str], kind: str) -> bool:
    if kind in ("gainer", "simplification"):
        return bounds_kind(r.get("elo0"), r.get("elo1")) == kind
    if kind in ("net", "search"):
        return r.get("net") == ("1" if kind == "net" else "0")
    return True


def weekly_finished_chart(finished: dict[str, dict[str, str]], now: datetime, theme: Theme,
                          weeks: int = 52, kind: str = "") -> str:
    """Tests finished per week, optionally only one kind of test (see WEEKLY_KINDS)."""
    week_list = _week_list(finished, now, weeks)
    by_week: dict[date, list] = defaultdict(list)
    for r in finished.values():
        if matches_kind(r, kind):
            by_week[_week_start(parse_iso(r["finished"]).date())].append(r)
    counts = [_outcome_counts(by_week[w]) for w in week_list]
    totals = [sum(c[i] for c in counts) for i in range(4)]

    what = f"{WEEKLY_KINDS[kind]} · " if kind else ""
    passed, sprt = totals[0] + totals[1], totals[0] + totals[1] + totals[2]
    rate = f" · {100 * passed / sprt:.0f}% passed" if sprt else ""
    body, header_y = _header(theme, _outcome_legend(theme, totals),
                             f"{what}{sum(totals):,} tests in {len(week_list)} weeks{rate}")
    columns = []
    for i, (w, c) in enumerate(zip(week_list, counts)):
        partial = i == len(week_list) - 1
        tip = (f"Week of {w:%b} {w.day}{' (so far)' if partial else ''}: {sum(c)} tests finished"
               + (f" — {_outcome_tip(c)}" if sum(c) else ""))
        columns.append(Column(_week_label(week_list, i), c, tip, partial))
    top = header_y + 24
    body += _columns(theme, _outcome_colors(theme), columns, PAD + 36, WIDTH - PAD, top, top + 200)

    label = f"{what}Tests finished per week over {len(week_list)} weeks: {_outcome_tip(totals)}"
    return _doc(WIDTH, top + 200 + 24 + PAD, theme, label, body)


def weekly_types_chart(checks: list[Check], now: datetime, theme: Theme, weeks: int = 52) -> str:
    """Average cores per week by test type, measured from the hourly snapshots."""
    by_week: dict[date, list[Check]] = defaultdict(list)
    for c in checks:
        if c.has_data:
            by_week[_week_start(c.time.date())].append(c)
    this_week = _week_start(now.date())
    first = max(min(by_week, default=this_week), this_week - timedelta(weeks=weeks - 1))
    week_list = [first + timedelta(weeks=i) for i in range((this_week - first).days // 7 + 1)]

    averages: list[list[float]] = []
    for w in week_list:
        snaps = by_week.get(w, [])
        averages.append([sum(getattr(c, _TYPE_ATTRS[k]) or 0 for c in snaps) / len(snaps) if snaps
                         else 0.0 for k in range(_N)])

    present = {k for avg in averages for k in range(_N) if avg[k] > 0}
    latest = sum(averages[-1]) if by_week.get(this_week) else None
    summary = f"{latest:,.0f} cores on average this week" if latest is not None else ""
    body, header_y = _header(theme, _type_items(theme, present), summary)
    columns = []
    for i, (w, avg) in enumerate(zip(week_list, averages)):
        snaps = len(by_week.get(w, []))
        partial = w == this_week
        split = ", ".join(f"{avg[k]:,.0f} {TEST_TYPES[k]}" for k in range(_N) if avg[k] >= 0.5)
        tip = (f"Week of {w:%b} {w.day}{' (so far)' if partial else ''}: {sum(avg):,.0f} cores on "
               f"average from {snaps} hourly snapshots — {split}" if snaps
               else f"Week of {w:%b} {w.day}: no snapshots")
        columns.append(Column(_week_label(week_list, i), avg, tip, partial))
    top = header_y + 24
    colors = [theme.types[k] for k in range(_N)]
    body += _columns(theme, colors, columns, PAD + 44, WIDTH - PAD, top, top + 200)
    label = f"Average cores per week by test type over {len(week_list)} weeks, from hourly snapshots"
    return _doc(WIDTH, top + 200 + 24 + PAD, theme, label, body)


# ---------------------------------------------------------------- throughput and runtimes

def throughput_chart(finished: dict[str, dict[str, str]], now: datetime, theme: Theme,
                     days: int = 14) -> str:
    """Tests finished per hour: faint hourly counts under a trailing 24-hour average.
    Only complete hours count, and tests stopped before playing a game are left out."""
    hour = timedelta(hours=1)
    end = now.replace(minute=0, second=0, microsecond=0)  # the current hour is still running
    start = end - timedelta(days=days)
    counts: dict[datetime, int] = defaultdict(int)
    for r in finished.values():
        if int(r.get("games") or 0) > 0:
            counts[parse_iso(r["finished"]).replace(minute=0, second=0)] += 1
    slots = [start + i * hour for i in range(days * 24)]
    hourly = [counts.get(s, 0) for s in slots]
    rolling = [sum(counts.get(s - j * hour, 0) for j in range(24)) / 24 for s in slots]

    color = theme.ink2
    header_y = PAD + 13
    body = [
        f'<rect x="{PAD}" y="{header_y - 9}" width="10" height="10" rx="2" fill="{color}" '
        f'fill-opacity="0.25"/>',
        _text(PAD + 16, header_y, "Finished in each hour", theme.ink2, 12),
    ]
    x = PAD + 16 + text_width("Finished in each hour", 12) + 18
    body += [_line(x, header_y - 4, x + 14, header_y - 4, color, 2),
             _text(x + 20, header_y, "24-hour average", theme.ink2, 12)]
    if slots:
        body.append(_text(WIDTH - PAD, header_y, f"{rolling[-1]:.2f} per hour now · "
                          f"{sum(hourly):,} in {days} days", theme.ink, 13, 600, "end"))

    ticks = nice_ticks(max(hourly + rolling, default=0), integer=True)
    frame = Frame(PAD + 44, WIDTH - PAD - 8, header_y + 24, header_y + 24 + 200, start, end, ticks[-1])
    body += _frame(theme, frame, ticks, compact, PAD, WIDTH - PAD, [])
    slot_w = (frame.right - frame.left) / max(1, len(slots))
    for i, c in enumerate(hourly):
        if c:
            x0 = frame.left + i * slot_w
            body.append(f'<rect x="{_n(x0 + slot_w * 0.15)}" y="{_n(frame.y(c))}" '
                        f'width="{_n(slot_w * 0.7)}" height="{_n(frame.bottom - frame.y(c))}" '
                        f'fill="{color}" fill-opacity="0.25"/>')
    points = " ".join(f"{_n(frame.left + (i + 0.5) * slot_w)},{_n(frame.y(v))}"
                      for i, v in enumerate(rolling))
    if points:
        body.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>')
        last_x, last_y = frame.left + (len(slots) - 0.5) * slot_w, frame.y(rolling[-1])
        body.append(f'<circle cx="{_n(last_x)}" cy="{_n(last_y)}" r="4" fill="{color}" '
                    f'stroke="{theme.surface}" stroke-width="2"/>')
    # Tooltips per day: hovering an hour column is too fiddly at this width.
    for d in range(days):
        day_slots = range(d * 24, (d + 1) * 24)
        first = slots[d * 24]
        tip = (f"{first:%b} {first.day}: {sum(hourly[i] for i in day_slots)} tests finished, "
               f"busiest hour {max(hourly[i] for i in day_slots)}")
        body.append(_hit(frame.left + d * 24 * slot_w, frame.top, 24 * slot_w,
                         frame.bottom - frame.top, tip))
    label = f"Tests finished per hour over the last {days} days, with a 24-hour average"
    return _doc(WIDTH, frame.bottom + 24 + PAD, theme, label, body)


RUNTIME_BUCKETS = [(1, "<1h"), (3, "1–3h"), (6, "3–6h"), (12, "6–12h"), (24, "12h–1d"),
                   (48, "1–2d"), (96, "2–4d"), (168, "4–7d"), (336, "1–2w"), (math.inf, "2w+")]


def _age(hours: float) -> str:
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.1f} days"


def runtime_chart(active: list[dict[str, str]], now: datetime, theme: Theme) -> str:
    """How long the running tests have been going (since they were submitted), by test type."""
    running = [r for r in active if _int(r, "cores") > 0 and r.get("started")]
    counts = [[0] * (_N + 1) for _ in RUNTIME_BUCKETS]
    ages = []
    for r in running:
        hours = (now - parse_iso(r["started"])).total_seconds() / 3600
        ages.append(hours)
        bucket = next(i for i, (limit, _) in enumerate(RUNTIME_BUCKETS) if hours < limit)
        counts[bucket][TYPE_INDEX.get(r["type"], _N)] += 1

    present = {k for c in counts for k in range(_N + 1) if c[k]}
    summary = (f"{len(running)} running tests · median {_age(statistics.median(ages))} since submitted"
               if ages else "")
    body, header_y = _header(theme, _type_items(theme, present), summary)
    columns = []
    for (_, label), c in zip(RUNTIME_BUCKETS, counts):
        split = ", ".join(f"{c[k]} {TEST_TYPES[k] if k < _N else 'other'}" for k in range(_N + 1) if c[k])
        tip = f"Running for {label}: {sum(c)} {'test' if sum(c) == 1 else 'tests'}" + (f" — {split}" if split else "")
        columns.append(Column(label, c, tip))
    top = header_y + 24
    colors = [_part_color(theme, k) for k in range(_N + 1)]
    body += _columns(theme, colors, columns, PAD + 36, WIDTH - PAD, top, top + 180)
    label = "Running tests by how long ago they were submitted, stacked by test type"
    return _doc(WIDTH, top + 180 + 24 + PAD, theme, label, body)


# ---------------------------------------------------------------- all charts

def _hourly_series(hourly: list[dict], key: str, index: int) -> dict[str, dict[datetime, float]]:
    """name -> {time: value} from the hourly breakdown (index 0 = cores, 1 = tests/workers)."""
    series: dict[str, dict[datetime, float]] = defaultdict(dict)
    for row in hourly:
        t = parse_iso(row["time"])
        for name, values in row.get(key, {}).items():
            series[name][t] = values[index]
    return series


def _daily_series(daily: list[dict[str, str]], checks: list[Check], kind: str,
                  column: str) -> dict[str, dict[datetime, float]]:
    """name -> {day at noon: average over the hours covered that day}."""
    covered: dict[date, float] = defaultdict(float)
    for c in checks:
        if c.has_data:
            covered[c.time.date()] += 1
    series: dict[str, dict[datetime, float]] = defaultdict(dict)
    for r in daily:
        if r["kind"] != kind or not r[column]:
            continue
        day = date.fromisoformat(r["date"])
        hours = min(24.0, covered.get(day, 24.0)) or 24.0
        series[r["name"]][datetime(day.year, day.month, day.day, 12, tzinfo=UTC)] = float(r[column]) / hours
    return series


def _tests_info(history: History) -> dict[str, dict[str, str]]:
    info = {r["id"]: r for r in history.finished.values()}
    info.update({r["id"]: r for r in history.active})
    return info


def render_all(history: History, now: datetime) -> dict[str, str]:
    """Every chart in both themes, keyed by file name."""
    h = history
    hourly_times = [parse_iso(row["time"]) for row in h.hourly]
    authors_cores = _hourly_series(h.hourly, "authors", 0)
    authors_tests = _hourly_series(h.hourly, "authors", 1)
    contributors = _hourly_series(h.hourly, "contributors", 0)
    long = has_long_history(h.checks)
    if long:
        days = daily_means(h.checks)
        day_times = [c.time for c in days]
        long_authors = _daily_series(h.daily, h.checks, "author", "core_hours")
        long_tests = _daily_series(h.daily, h.checks, "author", "count_hours")
        long_contributors = _daily_series(h.daily, h.checks, "contributor", "core_hours")

    files = {}
    for mode, theme in THEMES.items():
        files[f"tests-{mode}.svg"] = tests_chart(h.active, theme)
        files[f"authors-{mode}.svg"] = authors_chart(h.active, theme)
        files[f"contributors-{mode}.svg"] = contributors_chart(h.workers, h.active, theme)
        files[f"runtime-{mode}.svg"] = runtime_chart(h.active, now, theme)
        files[f"throughput-{mode}.svg"] = throughput_chart(h.finished, now, theme)
        files[f"types-{mode}.svg"] = types_chart(h.checks, now, theme)
        files[f"activity-{mode}.svg"] = activity_chart(h.checks, now, theme)
        files[f"finished-{mode}.svg"] = finished_chart(h.finished, now, theme)
        files[f"llr-{mode}.svg"] = llr_chart(h.llr, now, theme)
        files[f"weekly-finished-{mode}.svg"] = weekly_finished_chart(h.finished, now, theme)
        files[f"weekly-types-{mode}.svg"] = weekly_types_chart(h.checks, now, theme)
        for kind, name in (("gainer", "gainers"), ("simplification", "simplifications"),
                           ("net", "net"), ("search", "search")):
            files[f"weekly-{name}-{mode}.svg"] = weekly_finished_chart(h.finished, now, theme,
                                                                       kind=kind)
        files[f"tests-trend-{mode}.svg"] = tests_trend_chart(h.hourly, _tests_info(h), now, theme)
        files[f"authors-trend-{mode}.svg"] = entity_trend_chart(
            authors_cores, hourly_times, now, theme, days=14, max_gap=MAX_GAP, what="authors",
            unit="cores", label="Cores per author over the last 14 days")
        files[f"author-tests-trend-{mode}.svg"] = entity_trend_chart(
            authors_tests, hourly_times, now, theme, days=14, max_gap=MAX_GAP, what="authors",
            unit="tests", label="Running tests per author over the last 14 days")
        files[f"contributors-trend-{mode}.svg"] = entity_trend_chart(
            contributors, hourly_times, now, theme, days=14, max_gap=MAX_GAP, what="contributors",
            unit="cores", label="Cores contributed per user over the last 14 days")
        if long:
            files[f"long-types-{mode}.svg"] = types_chart(days, now, theme, 365, DAILY_GAP)
            files[f"long-activity-{mode}.svg"] = activity_chart(days, now, theme, 365, DAILY_GAP)
            files[f"long-authors-{mode}.svg"] = entity_trend_chart(
                long_authors, day_times, now, theme, days=365, max_gap=DAILY_GAP, what="authors",
                unit="cores", label="Average cores per author per day")
            files[f"long-author-tests-{mode}.svg"] = entity_trend_chart(
                long_tests, day_times, now, theme, days=365, max_gap=DAILY_GAP, what="authors",
                unit="tests", label="Average running tests per author per day", integer=False)
            files[f"long-contributors-{mode}.svg"] = entity_trend_chart(
                long_contributors, day_times, now, theme, days=365, max_gap=DAILY_GAP,
                what="contributors", unit="cores", label="Average cores contributed per user per day")
    return files
