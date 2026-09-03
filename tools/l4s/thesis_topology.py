#!/usr/bin/env python3
"""Compact topology and data-flow strips for the Persian thesis figures."""

from __future__ import annotations

from collections.abc import Callable

from matplotlib.axes import Axes
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


CLASSIC = "#b44b4b"
L4S = "#1976a3"
CUBIC = "#d69b2d"
INK = "#28323c"
LINK = "#89939d"
SWITCH_FILL = "#eef3f7"
HOST_FILL = "#fbfcfd"


def _node(axis: Axes, x: float, y: float, width: float, height: float,
          label: str, *, switch: bool = False, fontsize: float = 7.8) -> None:
    patch = FancyBboxPatch(
        (x - width / 2, y - height / 2), width, height,
        boxstyle="round,pad=0.012,rounding_size=0.018",
        linewidth=1.05, edgecolor=INK,
        facecolor=SWITCH_FILL if switch else HOST_FILL, zorder=5,
    )
    axis.add_patch(patch)
    axis.text(x, y, label, ha="center", va="center", fontsize=fontsize,
              linespacing=1.18, zorder=6)


def _arrow(axis: Axes, start: tuple[float, float], end: tuple[float, float],
           *, color: str, linewidth: float = 1.8,
           connectionstyle: str = "arc3") -> None:
    axis.add_patch(FancyArrowPatch(
        start, end, arrowstyle="-|>", mutation_scale=9.5,
        linewidth=linewidth, color=color, shrinkA=0, shrinkB=0,
        connectionstyle=connectionstyle, zorder=7,
    ))


def _single_flow_route(axis: Axes, segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...],
                       *, color: str) -> None:
    """Draw one routed flow, with an arrowhead only at its destination."""
    for start, end in segments[:-1]:
        axis.plot((start[0], end[0]), (start[1], end[1]), color=color,
                  linewidth=1.7, solid_capstyle="round", zorder=4)
    start, end = segments[-1]
    _arrow(axis, start, end, color=color, linewidth=1.7)


def _base(axis: Axes) -> None:
    axis.set_xlim(-.02, 1.02)
    axis.set_ylim(-.02, 1.02)
    axis.axis("off")


def draw_coexistence_topology(axis: Axes, fa: Callable[[str], str]) -> None:
    """Figures 6--8: one switch and alternative Reno/Prague foregrounds."""
    _base(axis)
    y = .50
    _node(axis, .10, y, .17, .34, "server\n" + fa("فرستنده"))
    _node(axis, .50, y, .22, .48,
          fa("سوییچ s1") + " (OVS)\nHTB + DualPI2\n" +
          fa("گلوگاه مشترک") + " 20 Mbit/s\n" + fa("روی هر دو خروجی"),
          switch=True, fontsize=7.3)
    _node(axis, .90, y, .17, .34, "client\n" + fa("گیرنده"))
    _node(axis, .10, .13, .17, .14, "bg_src", fontsize=7.1)
    _node(axis, .90, .13, .17, .14, "bg_sink", fontsize=7.1)

    for offset, color in ((.105, CLASSIC), (.035, L4S)):
        _arrow(axis, (.19, y + offset), (.385, y + offset), color=color)
        _arrow(axis, (.615, y + offset), (.81, y + offset), color=color)

    # The Cubic background is one routed flow with a single arrowhead at its
    # destination, rather than an arrowhead for every hop.
    _single_flow_route(axis, (
        ((.19, .17), (.385, .34)), ((.615, .34), (.81, .17)),
    ), color=CUBIC)

    axis.text(.50, .96,
              fa("جریان رو به جلو") + ": server -> s1 -> client",
              ha="center", va="center", fontsize=8.2, color=INK)
    axis.text(.50, .86, "Classic MoQ: server -> s1 -> client; Reno / Not-ECT",
              ha="center", va="center", fontsize=7.1, color=CLASSIC)
    axis.text(.50, .80, "L4S MoQ: server -> s1 -> client; Prague / ECT(1)",
              ha="center", va="center", fontsize=7.1, color=L4S)
    axis.text(.50, .20, fa("پس‌زمینه") + ": bg_src -> s1 -> bg_sink؛ " +
              "0/1/2/4 × TCP Cubic / Not-ECT",
              ha="center", va="center", fontsize=6.8, color=CUBIC)


def draw_dualpi2_validation_topology(axis: Axes, fa: Callable[[str], str]) -> None:
    """Figures 3--4: two competing TCP flows through a shared DualPI2 link."""
    _base(axis)
    _node(axis, .10, .68, .19, .22,
          "h1\n10.0.0.1\nPrague / ECT(1) / TCP 5301", fontsize=6.7)
    _node(axis, .10, .32, .19, .22,
          "h2\n10.0.0.2\nReno / ECT(0) / TCP 5302", fontsize=6.7)
    _node(axis, .37, .50, .20, .33,
          fa("سوییچ s1") + "\nDualPI2 " + fa("روی خروجی"),
          switch=True, fontsize=7.2)
    _node(axis, .63, .50, .20, .33,
          fa("سوییچ s2") + "\nDualPI2 " + fa("روی خروجی"),
          switch=True, fontsize=7.2)
    _node(axis, .90, .50, .19, .22,
          "server\n10.0.0.3\niperf3 " + fa("گیرنده"), fontsize=6.8)

    # Both coloured routes are concurrent TCP flows.  They share the central
    # bidirectional bottleneck but retain their endpoint/controller identity.
    for y, color in ((.57, L4S), (.43, CLASSIC)):
        _arrow(axis, (.195, .68 if color == L4S else .32), (.27, y), color=color)
        _arrow(axis, (.47, y), (.53, y), color=color)
        _arrow(axis, (.73, y), (.805, y), color=color)

    axis.text(.50, .88,
              fa("دو جریان TCP هم‌زمان از گلوگاه میان‌سوییچی مشترک عبور می‌کنند"),
              ha="center", va="center", fontsize=7.4, color=INK)
    axis.text(.94, .25,
              fa("گلوگاه دوسویهٔ مشترک میان s1 و s2 با ظرفیت 10 Mbit/s در هر جهت"),
              ha="right", va="center", fontsize=7.7, color=INK)
    axis.text(.94, .14,
              fa("در Linux، شکل‌دهی و AQM با qdisc اجرا می‌شود؛ در P4، برنامهٔ P4 روی هر دو سوییچ اجرا می‌شود"),
              ha="right", va="center", fontsize=7.0, color=INK)


def draw_pair_topology(axis: Axes, fa: Callable[[str], str], *,
                       show_classic: bool = True) -> None:
    """Figures 9, 10 and 15: one 3DGS flow over the two-switch path."""
    _base(axis)
    main_y = .53
    _node(axis, .08, main_y, .16, .30, "server\n" + fa("فرستنده"), fontsize=7.5)
    _node(axis, .35, main_y, .20, .40,
          fa("سوییچ s1") + " (OVS)\nDualPI2\n300 Mbit/s", switch=True, fontsize=7.3)
    s2_label = (fa("سوییچ s2") + " (OVS)\nClassic: FIFO\nL4S: DualPI2\n" +
                fa("خروجی رو به گیرنده") + " 100 Mbit/s"
                if show_classic else
                fa("سوییچ s2") + " (OVS)\nDualPI2\n" + fa("خروجی رو به گیرنده") + " 100 Mbit/s")
    _node(axis, .65, main_y, .22, .45, s2_label,
          switch=True, fontsize=7.0)
    _node(axis, .92, main_y, .16, .30, "client\n" + fa("گیرنده"), fontsize=7.5)
    _node(axis, .92, .88, .16, .15, "bg_sink", fontsize=7.1)
    _node(axis, .08, .12, .16, .15, "bg_src", fontsize=7.1)

    # These are alternative controller configurations for the same application
    # path, not simultaneous foreground streams.
    media_paths = ((.045, CLASSIC), (-.045, L4S)) if show_classic else ((0, L4S),)
    for offset, color in media_paths:
        _arrow(axis, (.16, main_y + offset), (.25, main_y + offset), color=color)
        _arrow(axis, (.45, main_y + offset), (.53, main_y + offset), color=color)
        _arrow(axis, (.77, main_y + offset), (.84, main_y + offset), color=color)

    # The orange route is one end-to-end TCP Cubic flow.  Plain connector
    # segments establish its switch path; only the final segment has an arrow.
    _single_flow_route(axis, (
        ((.16, .16), (.25, .32)), ((.45, .32), (.53, .32)),
        ((.77, .68), (.84, .82)),
    ), color=CUBIC)

    if show_classic:
        axis.text(.32, .88, fa("حالت کلاسیک") + ": Reno / Not-ECT",
                  ha="center", va="center", fontsize=7.0, color=CLASSIC)
        axis.text(.67, .88, "L4S: Prague / ECT(1)",
                  ha="center", va="center", fontsize=7.0, color=L4S)
    else:
        axis.text(.50, .88, "L4S: Prague / ECT(1)",
                  ha="center", va="center", fontsize=7.2, color=L4S)
    axis.text(.50, .245,
              fa("یک جریان 3DGS") + ": server -> s1 -> s2 -> client",
              ha="center", va="center", fontsize=8.0, color=INK)
    axis.text(.50, .025,
              fa("جریان پس‌زمینه") + ": bg_src -> s1 -> s2 -> bg_sink؛ " +
              "TCP Cubic / Not-ECT; RTT = 20 ms",
              ha="center", va="center", fontsize=7.0, color=INK)


def draw_spectrum_topology(axis: Axes, fa: Callable[[str], str]) -> None:
    """Figure 12: two-connection L4S-fraction spectrum with Reno background."""
    _base(axis)
    y = .55
    _node(axis, .08, y, .14, .28, "server\n" + fa("فرستنده"), fontsize=7.4)
    _node(axis, .33, y, .18, .38,
          fa("سوییچ s1") + " (OVS)\nDualPI2\n300 Mbit/s", switch=True, fontsize=7.2)
    _node(axis, .59, y, .18, .38,
          fa("سوییچ s2") + " (OVS)\nDualPI2\n50 Mbit/s", switch=True, fontsize=7.2)
    _node(axis, .87, y, .15, .28, "client\n" + fa("گیرنده"), fontsize=7.4)
    _node(axis, .87, .90, .17, .16, "bg_sink", fontsize=7.2)

    for offset, color in ((.055, CLASSIC), (-.035, L4S)):
        _arrow(axis, (.15, y + offset), (.24, y + offset), color=color)
        _arrow(axis, (.42, y + offset), (.50, y + offset), color=color)
        _arrow(axis, (.68, y + offset), (.795, y + offset), color=color)
    _arrow(axis, (.15, .72), (.24, .72), color=CUBIC, linewidth=1.5)
    _arrow(axis, (.42, .72), (.50, .72), color=CUBIC, linewidth=1.5)
    _arrow(axis, (.68, .70), (.79, .86), color=CUBIC, linewidth=1.5)

    axis.text(.25, .97, "Classic 3DGS (Reno / Not-ECT): server -> client",
              ha="center", va="center", fontsize=5.45, color=CLASSIC)
    axis.text(.57, .97, "L4S 3DGS (Prague / ECT(1)): server -> client",
              ha="center", va="center", fontsize=5.45, color=L4S)
    axis.text(.48, .09,
              "Background: server -> bg_sink; TCP Reno / Not-ECT; 280 Mbit/s; RTT = 20 ms",
              ha="center", va="center", fontsize=6.85, color=CUBIC)
