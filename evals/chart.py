"""The learning-curve chart (spec §9, Ring 3.6).

> The headline chart: resolution rate vs. corpus size, real vs. negative control, with the
> Ring 0/1 baselines overlaid.

Inline SVG, generated from the same committed JSON the report's tables read. No plotting
library and no image file: a chart that is a `<img src="chart.png">` can fall out of step with
the table beside it, and nobody notices because they render from different sources. Here the
only way for the line and the number to disagree is for the code to be wrong about both.

Drawn deliberately plainly. The one visual decision that carries meaning is that the control
is drawn in the same weight as the treatment rather than as a faint dashed afterthought — it
is the line that decides whether the other one means anything, and de-emphasising it would be
an editorial claim about the result.
"""

WIDTH, HEIGHT = 720, 380
PAD_L, PAD_R, PAD_T, PAD_B = 56, 150, 24, 44

INK = "#14181f"
GRID = "#e6e3dd"
MUTED = "#6b7280"
TREATMENT = "#1a5fb4"
CONTROL = "#9a6a00"
COUNTERPARTY = "#1a7f4b"
BASELINE = "#8a8a8a"


def _scale(points: list[dict]):
    xs = [p["corpus_size"] for p in points]
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1

    def x_of(corpus: int) -> float:
        return PAD_L + (corpus - lo) / span * (WIDTH - PAD_L - PAD_R)

    def y_of(rate: float) -> float:
        return PAD_T + (1.0 - rate) * (HEIGHT - PAD_T - PAD_B)

    return x_of, y_of


def _polyline(points, x_of, y_of, key, colour, dash=""):
    coords = " ".join(
        f"{x_of(p['corpus_size']):.1f},{y_of(v):.1f}"
        for p in points
        if (v := key(p)) is not None
    )
    stroke = f' stroke-dasharray="{dash}"' if dash else ""
    dots = "".join(
        f'<circle cx="{x_of(p["corpus_size"]):.1f}" cy="{y_of(v):.1f}" r="3.5" '
        f'fill="{colour}"/>'
        for p in points
        if (v := key(p)) is not None
    )
    return (
        f'<polyline points="{coords}" fill="none" stroke="{colour}" stroke-width="2.2"'
        f'{stroke} stroke-linejoin="round"/>{dots}'
    )


def render_curve_svg(curve: dict, rules_baseline: float | None = None) -> str:
    """One chart from one result file. Returns inline SVG."""
    points = curve.get("curve") or []
    if len(points) < 2:
        return '<p class="missing">Not enough snapshots to plot a curve.</p>'

    x_of, y_of = _scale(points)
    parts = []

    # Horizontal gridlines at every 20%, labelled. Enough to read a value off, few enough
    # not to compete with the lines.
    for tick in range(0, 6):
        rate = tick / 5
        y = y_of(rate)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{WIDTH - PAD_R}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
            f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{MUTED}">{rate:.0%}</text>'
        )

    # x axis: one label per snapshot, showing deposits rather than corpus size, because
    # deposits are the thing being varied.
    for p in points:
        x = x_of(p["corpus_size"])
        parts.append(
            f'<text x="{x:.1f}" y="{HEIGHT - PAD_B + 18}" text-anchor="middle" '
            f'font-size="11" fill="{MUTED}">{p["deposits"]}</text>'
        )
    parts.append(
        f'<text x="{(PAD_L + WIDTH - PAD_R) / 2:.0f}" y="{HEIGHT - 6}" '
        f'text-anchor="middle" font-size="11.5" fill="{INK}">deposited precedents</text>'
    )

    if rules_baseline is not None:
        y = y_of(rules_baseline)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{WIDTH - PAD_R}" y2="{y:.1f}" '
            f'stroke="{BASELINE}" stroke-width="1.5" stroke-dasharray="2 4"/>'
        )

    parts.append(_polyline(points, x_of, y_of,
                           lambda p: p["retrieved"]["counterparty_resolution_rate"],
                           COUNTERPARTY))
    parts.append(_polyline(points, x_of, y_of,
                           lambda p: p["random_control"]["resolution_rate"], CONTROL))
    parts.append(_polyline(points, x_of, y_of,
                           lambda p: p["retrieved"]["resolution_rate"], TREATMENT))

    legend = [
        (TREATMENT, "retrieval-grounded", None),
        (CONTROL, "random control", None),
        (COUNTERPARTY, "counterparty subset", None),
    ]
    if rules_baseline is not None:
        legend.append((BASELINE, f"rules only ({rules_baseline:.1%})", "2 4"))

    for index, (colour, label, dash) in enumerate(legend):
        y = PAD_T + 14 + index * 21
        x = WIDTH - PAD_R + 14
        stroke = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<line x1="{x}" y1="{y}" x2="{x + 20}" y2="{y}" stroke="{colour}" '
            f'stroke-width="2.4"{stroke}/>'
            f'<text x="{x + 27}" y="{y + 4}" font-size="11.5" fill="{INK}">{label}</text>'
        )

    return (
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" '
        f'role="img" aria-label="Resolution rate against corpus size, with the random '
        f'control and the counterparty subset" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="system-ui, sans-serif">'
        + "".join(parts)
        + "</svg>"
    )
