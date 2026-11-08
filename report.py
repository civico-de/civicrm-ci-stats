#!/usr/bin/env python3
"""Render docs/index.html from data/: detected changes first, charts as evidence below."""
import argparse
import html
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import median

import collect
import detect

MAX_DAILY_DAYS = 120  # a longer history is charted per week
CHANGES_ON_OVERVIEW = 20
TABLE_DAYS = 14
WIDTH, HEIGHT = 640, 200
LEFT, RIGHT, TOP, BOTTOM = 62, 12, 12, 26
MINUTE_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200)
COUNT_STEPS = (1, 2, 5, 10, 20, 50, 100)
SERIES_SLOTS = 8

STYLE = """
.viz-root{color-scheme:light;--surface:#fcfcfb;--text:#0b0b0b;--text-2:#52514e;--grid:#e6e5e0;--line:#b5b4ad;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface:#1a1a19;
--text:#f0efec;--text-2:#c3c2b7;--grid:#333330;--line:#62615c;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;
--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface:#1a1a19;--text:#f0efec;--text-2:#c3c2b7;--grid:#333330;
--line:#62615c;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}
*{box-sizing:border-box}html,body{margin:0}body{background:#fcfcfb}
@media (prefers-color-scheme:dark){body{background:#1a1a19}}
.viz-root{background:var(--surface);color:var(--text);font:15px/1.5 system-ui,sans-serif;max-width:1340px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:32px 0 8px}h3{font-size:15px;margin:0}
p{margin:4px 0 12px;max-width:80ch}.sub{color:var(--text-2);font-size:13px}a{color:inherit}
.change{border-top:1px solid var(--line);padding:12px 0}.change b{font-size:17px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 6px;font-size:13px;margin-left:6px}
.filters{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:8px 0 16px}
.filters label{display:inline-flex;gap:6px;align-items:center}.swatch{width:12px;height:12px;border-radius:50%;display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr));gap:24px}
figure{margin:0;min-width:0;max-width:640px}svg{width:100%;height:auto;display:block}
svg text{fill:var(--text-2);font-size:12px}.gridline{stroke:var(--grid)}.series{fill:none;stroke-width:2}
.mark{stroke:var(--line);stroke-dasharray:3 3}.hit{fill:transparent}
nav{display:flex;flex-wrap:wrap;gap:4px 12px;margin:8px 0 16px}nav [aria-current]{font-weight:700;text-decoration:none}
details{margin-top:4px;font-size:13px}table{border-collapse:collapse;width:100%}td,th{padding:2px 8px 2px 0;text-align:left;border-bottom:1px solid var(--grid)}
.scroll{overflow-x:auto}#tip{position:fixed;display:none;pointer-events:none;background:var(--text);color:var(--surface);padding:4px 8px;border-radius:4px;font-size:13px}
"""

SCRIPT = """
const tip=document.getElementById('tip');
document.addEventListener('mouseover',e=>{const t=e.target.dataset&&e.target.dataset.tip;
 if(t){tip.textContent=t;tip.style.display='block';}else{tip.style.display='none';}});
document.addEventListener('mousemove',e=>{tip.style.left=Math.min(e.clientX+12,innerWidth-260)+'px';tip.style.top=(e.clientY+12)+'px';});
document.querySelectorAll('[data-filter]').forEach(box=>box.addEventListener('change',()=>{
 document.querySelectorAll('[data-node="'+box.dataset.filter+'"]').forEach(n=>{n.style.display=box.checked?'':'none';});}));
"""


def esc(value):
    return html.escape(str(value), quote=True)


def fmt_seconds(value):
    minutes, seconds = divmod(round(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


def fmt_tick(value):
    hours, minutes = divmod(int(value) // 60, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def tick_values(highest, steps):
    """Return round axis values from zero that reach the highest value in at most five ticks."""
    step = next((step for step in steps if highest <= step * 4), steps[-1])
    count = int(highest // step) + 1
    return [step * index for index in range(count + 1)]


def fmt_time(moment):
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def usual_config(jobs):
    """Return the most frequent (bldtype, bkprof) per suite, so each chart compares like with like."""
    counts = defaultdict(Counter)
    for job in jobs:
        counts[job["suite"]][(job["bldtype"], job["bkprof"])] += 1
    return {suite: counter.most_common(1)[0][0] for suite, counter in counts.items()}


def week_start(day):
    return day - timedelta(days=day.weekday())


def medians_per(period, rows):
    days = defaultdict(list)
    for day, value in rows:
        days[period(day)].append(value)
    return {day: (median(values), len(values)) for day, values in sorted(days.items())}


def line_chart(title, subtitle, series, colors, changes, formatter, node_series, first_day, last_day, axis=(MINUTE_STEPS, fmt_tick)):
    """Return one SVG line chart with a table view; series maps a name to {day: (value, n)}."""
    values = [value for days in series.values() for value, _ in days.values()]
    steps, tick_label = axis
    ticks = tick_values(max(values), steps)
    top = ticks[-1]
    span = max((last_day - first_day).days, 1)
    x = lambda day: LEFT + (WIDTH - LEFT - RIGHT) * (day - first_day).days / span
    y = lambda value: HEIGHT - BOTTOM - (HEIGHT - TOP - BOTTOM) * value / top
    parts = []
    for value in ticks:
        parts.append(f'<line class="gridline" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y(value):.1f}" y2="{y(value):.1f}"/>')
        parts.append(f'<text x="{LEFT - 6}" y="{y(value) + 4:.1f}" text-anchor="end">{esc(tick_label(value))}</text>')
    for tick in range(4):
        day = first_day + timedelta(days=round(span * tick / 3))
        anchor = "start" if tick == 0 else "end" if tick == 3 else "middle"
        label = day.strftime("%b %Y" if span > 365 else "%b %d")
        parts.append(f'<text x="{x(day):.1f}" y="{HEIGHT - 8}" text-anchor="{anchor}">{label}</text>')
    for change in changes:
        day = change["at"].date()
        if first_day <= day <= last_day:
            parts.append(f'<line class="mark" x1="{x(day):.1f}" x2="{x(day):.1f}" y1="{TOP}" y2="{HEIGHT - BOTTOM}"/>')
    for name, days in sorted(series.items()):
        node = f' data-node="{esc(name)}"' if node_series else ""
        color = colors[name]
        points = " ".join(f"{x(day):.1f},{y(value):.1f}" for day, (value, _) in days.items())
        group = [f'<g{node}><polyline class="series" points="{points}" style="stroke:{color}"/>']
        for day, (value, count) in days.items():
            tip = f"{day.isoformat()} · {name} · {formatter(value)} (n={count})"
            group.append(f'<circle cx="{x(day):.1f}" cy="{y(value):.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="2"/>')
            group.append(f'<circle class="hit" cx="{x(day):.1f}" cy="{y(value):.1f}" r="10" data-tip="{esc(tip)}"/>')
        parts.append("".join(group) + "</g>")
    table_days = sorted({day for days in series.values() for day in days})[-TABLE_DAYS:]
    names = sorted(series)
    head = "".join(f"<th>{esc(name)}</th>" for name in names)
    body = "".join(
        f"<tr><td>{day.isoformat()}</td>" + "".join(
            f"<td>{esc(formatter(series[name][day][0])) if day in series[name] else '–'}</td>" for name in names
        ) + "</tr>"
        for day in reversed(table_days)
    )
    return (
        f"<figure><h3>{esc(title)}</h3><div class=\"sub\">{esc(subtitle)}</div>"
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{esc(title)}: median over time, table below">{"".join(parts)}</svg>'
        f'<details><summary>Table (latest {TABLE_DAYS} values)</summary><div class="scroll"><table><tr><th>Day</th>{head}</tr>{body}</table></div></details></figure>'
    )


def change_html(change):
    sign = "faster" if change["change"] < 0 else "slower"
    tags = []
    if change["control_moved"]:
        tags.append('<span class="tag">Likely host load: the Karma control moved too</span>')
    if change["tests_changed"]:
        tags.append('<span class="tag">Test count changed</span>')
    prs = "".join(
        f'<li><a href="https://github.com/civicrm/civicrm-core/pull/{esc(m["number"])}">#{esc(m["number"])}</a> '
        f'{esc(m["title"])} <span class="sub">merged {esc(fmt_time(m["at"]))} · fits {m["fit"] * 100:.0f}% of the runs</span></li>'
        for m in change["candidates"]
    )
    more = f'<li class="sub">and {change["more_candidates"]} less likely</li>' if change["more_candidates"] else ""
    prs_block = f"<ul>{prs}{more}</ul>" if prs else '<p class="sub">No merge recorded in this window.</p>'
    return (
        f'<div class="change"><b>{abs(change["change"]) * 100:.0f}% {sign}</b> around {esc(fmt_time(change["at"]))}{"".join(tags)}'
        f'<div class="sub">{change["series"]} series · suites: {esc(", ".join(change["suites"]))} · hosts: {esc(", ".join(change["nodes"]))} · '
        f'parallel jobs on the host (median): {change["parallel_before"]:g} before, {change["parallel_after"]:g} after</div>'
        f'<div class="sub">Merged between {esc(fmt_time(change["low"]))} and {esc(fmt_time(change["high"]))} (candidates, not established causes):</div>{prs_block}</div>'
    )


def batch_totals(jobs, config):
    """Return (day, seconds) for batches where every usual-config PHPUnit suite succeeded."""
    expected = {suite for suite in config if "phpunit" in suite}
    batches = defaultdict(dict)
    for job in jobs:
        if "phpunit" in job["suite"] and (job["bldtype"], job["bkprof"]) == config[job["suite"]]:
            batches[job["batch_id"]][job["suite"]] = job
    return [
        (min(detect.parse_time(job["started"]) for job in found.values()).date(), sum(float(job["duration"]) for job in found.values()))
        for found in batches.values() if set(found) == expected
    ]


def month_of(job):
    return job["started"][:7]


def month_nav(months, current):
    """Link the overview and every month; the current page carries aria-current."""
    entries = [("index.html", "Overview", current is None)] + [(f"{month}.html", month, month == current) for month in reversed(months)]
    current_mark = ' aria-current="page"'
    links = "".join(f'<a href="{href}"{current_mark if active else ""}>{label}</a>' for href, label, active in entries)
    return f'<nav aria-label="Pages">{links}</nav>'


def render_site(jobs, merges, runs):
    """Return {file name: page}: one overview of the whole history and one page per month."""
    changes = detect.find_changes(jobs, merges)
    months = sorted({month_of(job) for job in jobs if job["started"]})
    last_run = runs[-1]["at"] if runs else "never"
    recent = changes[:CHANGES_ON_OVERVIEW]
    older = f'<p class="sub">{len(changes) - len(recent)} older changes are on the month pages.</p>' if len(changes) > len(recent) else ""
    site = {"index.html": render_page("CiviCRM CI Stats", f"Last collected: {last_run}. ", jobs, recent, changes, older, month_nav(months, None))}
    for month in months:
        in_month = [change for change in changes if change["at"].strftime("%Y-%m") == month]
        site[f"{month}.html"] = render_page(f"CiviCRM CI Stats {month}", "", [job for job in jobs if month_of(job) == month], in_month, in_month, "", month_nav(months, month))
    return site


def render_page(heading, stamp, jobs, listed, marked, older, nav):
    """Render one page: listed changes as text, marked changes as dashed lines in the charts."""
    comparable = [job for job in jobs if detect.is_comparable(job)]
    changes_html = "".join(change_html(change) for change in listed) or '<p>No lasting change detected in the collected data.</p>'
    charts, filters = "", ""
    if comparable:
        days_seen = [detect.parse_time(job["started"]).date() for job in comparable]
        last = max(days_seen)
        period, unit = (week_start, "week") if (last - min(days_seen)).days > MAX_DAILY_DAYS else (lambda day: day, "day")
        first = period(min(days_seen))
        nodes = sorted({job["node"] for job in comparable})
        colors = {node: f"var(--s{index + 1})" for index, node in enumerate(nodes[:SERIES_SLOTS])}
        for node in nodes[SERIES_SLOTS:]:
            colors[node] = "var(--text-2)"
        filters = '<div class="filters" role="group" aria-label="Show hosts">Hosts:' + "".join(
            f'<label><input type="checkbox" checked data-filter="{esc(node)}"><span class="swatch" style="background:{colors[node]}"></span>{esc(node)}</label>'
            for node in nodes
        ) + "</div>"
        config = usual_config(comparable)
        totals = medians_per(period, batch_totals(comparable, config))
        if totals:
            charts += '<h2>All PHPUnit suites, per PR batch</h2>' + line_chart(
                "Summed test time per batch", f"Median per {unit} of complete, successful batches. Dashed lines mark detected changes.",
                {"all": totals}, {"all": "var(--s1)"}, marked, fmt_seconds, False, first, last)
        grid = ""
        for suite in sorted(config):
            selected = [job for job in comparable if job["suite"] == suite and (job["bldtype"], job["bkprof"]) == config[suite]]
            series = {node: medians_per(period, [(detect.parse_time(j["started"]).date(), float(j["duration"])) for j in selected if j["node"] == node]) for node in nodes}
            series = {node: days for node, days in series.items() if days}
            grid += line_chart(suite, f"{config[suite][0]}, {config[suite][1]} · median per {unit} and host", series, colors, marked, fmt_seconds, True, first, last)
        charts += f'<h2>Per suite and host</h2><div class="grid">{grid}</div>'
        load = defaultdict(list)
        for job in jobs:
            if job.get("parallel") is not None and job["started"] and job["node"] in colors:
                load[job["node"]].append((detect.parse_time(job["started"]).date(), job["parallel"]))
        if load:
            charts += '<h2>Load per host</h2>' + line_chart(
                "Other jobs running at the same time", f"Median per {unit}, all batch types. Jobs outside Duderino are not visible.",
                {node: medians_per(period, rows) for node, rows in load.items()}, colors, marked, lambda v: f"{v:g}", True, first, last, axis=(COUNT_STEPS, lambda v: f"{v:g}"))
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(heading)}</title><style>{STYLE}</style></head><body><div class=\"viz-root\">"
        f"<h1>{esc(heading)}</h1><p class=\"sub\">Runtime of civicrm-core PR tests on test.civicrm.org. {esc(stamp)}"
        f"{len(comparable)} comparable jobs of {len(jobs)} collected.</p>{nav}"
        f"<h2>Detected changes</h2><p class=\"sub\">Successful PR runs against master only. Test time reflects the test harness as well as the code, "
        f"so a change here is a lead, not a measurement of CiviCRM's own speed. A change needs {detect.WINDOW} runs per host after it before it can appear.</p>"
        f"{changes_html}{older}{filters}{charts}<div id=\"tip\" role=\"status\"></div></div><script>{SCRIPT}</script></body></html>\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).parent
    parser.add_argument("--data", type=Path, default=root / "data")
    parser.add_argument("--out", type=Path, default=root / "docs", help="directory for the pages")
    args = parser.parse_args()
    site = render_site(collect.read_jobs(args.data), collect.read_csv(args.data / "merges.csv"), collect.read_csv(args.data / "runs.csv"))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, page in site.items():
        (args.out / name).write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
