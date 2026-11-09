#!/usr/bin/env python3
"""Render the pages in docs/ from data/: the latest change first, charts as evidence below."""
import argparse
import html
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import median

import collect
import detect

MAX_DAILY_DAYS = 120  # a longer history is charted per week
CHANGES_ON_OVERVIEW = 20
WIDTH, HEIGHT = 640, 220
LEFT, RIGHT, TOP, BOTTOM = 74, 12, 14, 28
MINUTE_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200)
COUNT_STEPS = (1, 2, 5, 10, 20, 50, 100)
SERIES_SLOTS = 8
UNKNOWN_HOST = "host unknown"

LIGHT = """color-scheme:light;--surface:#fcfcfb;--text:#0b0b0b;--text-2:#52514e;--grid:#e6e5e0;--line:#b5b4ad;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948"""
DARK = """color-scheme:dark;--surface:#1a1a19;--text:#f0efec;--text-2:#c3c2b7;--grid:#333330;--line:#62615c;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767"""

STYLE = f":root{{{LIGHT}}}@media (prefers-color-scheme:dark){{:root:not([data-theme=\"light\"]){{{DARK}}}}}:root[data-theme=\"dark\"]{{{DARK}}}" + """
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--surface);color:var(--text);font:16px/1.5 system-ui,sans-serif}
.page{max-width:1340px;margin:0 auto;padding:24px 16px 48px}
a{color:inherit}:focus-visible{outline:2px solid var(--text);outline-offset:2px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:4px 32px}
h1{font-size:17px;margin:0}h2{font-size:21px;line-height:1.25;margin:40px 0 8px}h3{font-size:16px;margin:0}
nav{display:flex;flex-wrap:wrap;gap:0 4px}nav a{padding:4px 8px}nav [aria-current]{font-weight:700;text-decoration:none}
.lede{color:var(--text-2);margin:0 0 32px;max-width:72ch}
.headline{font-size:clamp(30px,5.5vw,52px);line-height:1.1;font-weight:650;letter-spacing:-.015em;margin:0 0 20px;max-width:24ch;text-wrap:balance}
.sub{color:var(--text-2);font-size:14px}p{margin:4px 0 12px;max-width:72ch}
.change{display:grid;gap:4px 32px;border-top:1px solid var(--line);padding:16px 0;scroll-margin-top:16px}
.delta{font-size:24px;line-height:1.2;font-weight:650;margin:0}.when{margin:0}
.change ul{margin:4px 0 8px;padding:0;list-style:none}.change li{margin:0 0 6px;max-width:72ch}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 6px;font-size:14px;margin-top:6px}
@media (min-width:760px){.change{grid-template-columns:15rem 1fr}}
.hosts{margin-top:40px}
.filters{position:sticky;top:0;z-index:1;background:var(--surface);display:flex;flex-wrap:wrap;gap:0 16px;align-items:center;padding:8px 0;border-bottom:1px solid var(--grid)}
.filters label{display:inline-flex;gap:6px;align-items:center;min-height:32px;cursor:pointer}
.key{width:16px;height:3px;border-radius:2px;display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr));gap:32px 24px}
figure{margin:0;min-width:0;max-width:640px}.hero{max-width:1040px}
svg{width:100%;height:auto;display:block;margin-top:4px;touch-action:pan-y}svg:focus-visible{outline-offset:4px}
svg text{fill:var(--text-2);font-size:17px;font-variant-numeric:tabular-nums}
svg line,svg polyline,.dot{vector-effect:non-scaling-stroke}
.gridline{stroke:var(--grid)}.series{fill:none;stroke-width:2}.mark{stroke:var(--line);stroke-dasharray:3 3}
.cross{stroke:var(--text-2);display:none}.col{fill:transparent}
@media (min-width:1072px){.hero svg text{font-size:8px}}
@media (min-width:700px) and (max-width:1071px){.hero svg text{font-size:10px}}
@media (max-width:480px){svg text{font-size:21px}.dot{r:6px}}
details{margin-top:4px;font-size:14px}summary{cursor:pointer;min-height:24px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}
td,th{padding:2px 12px 2px 0;text-align:left;border-bottom:1px solid var(--grid);white-space:nowrap}.num{text-align:right;font-variant-numeric:tabular-nums}
footer{margin-top:48px;padding-top:12px;border-top:1px solid var(--line)}
#tip{position:fixed;z-index:2;display:none;pointer-events:none;background:var(--text);color:var(--surface);padding:6px 10px;border-radius:4px;font-size:14px}
#tip div{display:flex;align-items:center;gap:8px}#tip b{font-variant-numeric:tabular-nums}#tip small{font-size:12px}
"""

SCRIPT = """
const tip=document.getElementById('tip'),hidden=new Set(),charts=[...document.querySelectorAll('svg[data-chart]')];
function place(x,y){tip.style.left=Math.max(4,Math.min(x+14,innerWidth-tip.offsetWidth-8))+'px';tip.style.top=Math.max(4,Math.min(y+14,innerHeight-tip.offsetHeight-8))+'px';}
function row(line){const [name,color,value,count]=line.split('\\t');if(hidden.has(name))return null;
 const div=document.createElement('div'),key=document.createElement('span'),b=document.createElement('b'),label=document.createElement('span'),small=document.createElement('small');
 key.className='key';key.style.background=color;b.textContent=value;label.textContent=name;small.textContent=count;div.append(key,b,label,small);return div;}
function show(col,x,y){const lines=col.dataset.tip.split('\\n'),head=document.createElement('div');head.textContent=col.dataset.day;
 tip.replaceChildren(head,...lines.map(row).filter(Boolean));tip.style.display='block';place(x,y);
 charts.forEach(chart=>{const match=chart.querySelector('.col[data-day="'+col.dataset.day+'"]'),cross=chart.querySelector('.cross');
  cross.style.display=match?'block':'none';if(match){cross.setAttribute('x1',match.dataset.x);cross.setAttribute('x2',match.dataset.x);}});}
function hide(){tip.style.display='none';charts.forEach(chart=>{chart.querySelector('.cross').style.display='none';});}
document.addEventListener('pointerover',e=>{const col=e.target.closest&&e.target.closest('.col');if(col){show(col,e.clientX,e.clientY);}else{hide();}});
document.addEventListener('pointermove',e=>{if(tip.style.display==='block'){place(e.clientX,e.clientY);}});
charts.forEach(chart=>{const cols=[...chart.querySelectorAll('.col')];let index=cols.length-1;
 const step=delta=>{index=Math.max(0,Math.min(cols.length-1,index+delta));const box=cols[index].getBoundingClientRect();show(cols[index],box.left,box.top);};
 chart.addEventListener('focus',()=>step(0));chart.addEventListener('blur',hide);
 chart.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();step(e.key==='ArrowRight'?1:-1);}if(e.key==='Escape'){hide();}});});
const groups=[...document.querySelectorAll('g[data-node]')];
document.querySelectorAll('[data-filter]').forEach(box=>{const name=box.dataset.filter,label=box.closest('label');
 box.addEventListener('change',()=>{if(box.checked){hidden.delete(name);}else{hidden.add(name);}groups.filter(n=>n.dataset.node===name).forEach(n=>{n.style.display=box.checked?'':'none';});});
 const emphasize=on=>groups.forEach(n=>{n.style.opacity=on&&n.dataset.node!==name?'.2':'';});
 label.addEventListener('pointerenter',()=>emphasize(true));label.addEventListener('pointerleave',()=>emphasize(false));});
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


def change_label(change):
    return f"{'−' if change['change'] < 0 else '+'}{abs(change['change']) * 100:.0f}%"


def axes_svg(ticks, tick_label, y, x, first_day, last_day):
    parts = []
    for value in ticks:
        parts.append(f'<line class="gridline" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y(value):.1f}" y2="{y(value):.1f}"/>')
        parts.append(f'<text x="{LEFT - 6}" y="{y(value) + 5:.1f}" text-anchor="end">{esc(tick_label(value))}</text>')
    span = (last_day - first_day).days
    for offset in sorted({round(span * tick / 3) for tick in range(4)}):
        day = first_day + timedelta(days=offset)
        anchor = "start" if offset == 0 else "end" if offset == span else "middle"
        label = day.strftime("%b %Y" if span > 365 else "%b %d")
        parts.append(f'<text x="{x(day):.1f}" y="{HEIGHT - 6}" text-anchor="{anchor}">{label}</text>')
    return "".join(parts)


def marks_svg(changes, first_day, last_day, x):
    days = [change["at"].date() for change in changes]
    return "".join(f'<line class="mark" x1="{x(day):.1f}" x2="{x(day):.1f}" y1="{TOP}" y2="{HEIGHT - BOTTOM}"/>' for day in days if first_day <= day <= last_day)


def series_svg(series, colors, x, y, node_series):
    parts = []
    for name, days in sorted(series.items()):
        node = f' data-node="{esc(name)}"' if node_series else ""
        color = colors[name]
        points = " ".join(f"{x(day):.1f},{y(value):.1f}" for day, (value, _) in days.items())
        dots = "".join(f'<circle class="dot" cx="{x(day):.1f}" cy="{y(value):.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="2"/>' for day, (value, _) in days.items())
        parts.append(f'<g{node}><polyline class="series" points="{points}" style="stroke:{color}"/>{dots}</g>')
    return "".join(parts)


def columns_svg(series, colors, x, formatter):
    """Add one invisible column per date; its data feeds the crosshair and the tooltip."""
    days = sorted({day for values in series.values() for day in values})
    centers = [x(day) for day in days]
    edges = [LEFT, *((a + b) / 2 for a, b in pairwise(centers)), WIDTH - RIGHT]
    parts = [f'<line class="cross" x1="0" x2="0" y1="{TOP}" y2="{HEIGHT - BOTTOM}"/>']
    for index, day in enumerate(days):
        rows = "\n".join(f"{name}\t{colors[name]}\t{formatter(series[name][day][0])}\t(n={series[name][day][1]})" for name in sorted(series) if day in series[name])
        parts.append(
            f'<rect class="col" x="{edges[index]:.1f}" y="{TOP}" width="{edges[index + 1] - edges[index]:.1f}" height="{HEIGHT - TOP - BOTTOM}" '
            f'data-day="{day.isoformat()}" data-x="{centers[index]:.1f}" data-tip="{esc(rows)}"/>'
        )
    return "".join(parts)


def values_table(series, formatter):
    names = sorted(series)
    head = "".join(f'<th class="num">{esc(name)}</th>' for name in names)
    body = "".join(
        f"<tr><td>{day.isoformat()}</td>" + "".join(
            f'<td class="num">{esc(formatter(series[name][day][0])) if day in series[name] else "–"}</td>' for name in names
        ) + "</tr>"
        for day in sorted({day for days in series.values() for day in days}, reverse=True)
    )
    return f'<details><summary>Table of values</summary><div class="scroll"><table><tr><th>Date</th>{head}</tr>{body}</table></div></details>'


def line_chart(title, subtitle, series, colors, changes, formatter, node_series, first_day, last_day, axis=(MINUTE_STEPS, fmt_tick), hero=False):
    """Return one SVG line chart with a table view; series maps a name to {day: (value, n)}."""
    steps, tick_label = axis
    ticks = tick_values(max(value for days in series.values() for value, _ in days.values()), steps)
    span = max((last_day - first_day).days, 1)
    x = lambda day: LEFT + (WIDTH - LEFT - RIGHT) * (day - first_day).days / span
    y = lambda value: HEIGHT - BOTTOM - (HEIGHT - TOP - BOTTOM) * value / ticks[-1]
    drawing = axes_svg(ticks, tick_label, y, x, first_day, last_day) + marks_svg(changes, first_day, last_day, x) + series_svg(series, colors, x, y, node_series) + columns_svg(series, colors, x, formatter)
    label = f"{title}: median over time. Arrow keys step through the dates; the table holds the same values."
    return (
        f'<figure{" class=hero" if hero else ""}><h3>{esc(title)}</h3><div class="sub">{esc(subtitle)}</div>'
        f'<svg data-chart viewBox="0 0 {WIDTH} {HEIGHT}" role="img" tabindex="0" aria-label="{esc(label)}">{drawing}</svg>'
        f"{values_table(series, formatter)}</figure>"
    )


def change_title(change):
    """Name the median change of the affected series' duration, which is not a change of the whole batch."""
    return f'{"down" if change["change"] < 0 else "up"} {abs(change["change"]) * 100:.0f}%'


def headline(listed, measured):
    if not listed:
        if not measured:
            return "No comparable jobs in this period."
        if any(job["node"] for job in measured):
            return "No lasting change detected in the collected data."
        return "Not checked for changes: these jobs have no recorded host."
    latest = listed[0]
    scope = latest["suites"][0] if len(latest["suites"]) == 1 else f'{len(latest["suites"])} suites'
    return f'Latest change: test time {change_title(latest)} in {scope} around {latest["at"].strftime("%Y-%m-%d")}'


def evidence_table(change):
    rows = "".join(
        f'<tr><td>{esc(row["suite"])}</td><td>{esc(row["bldtype"])}, {esc(row["bkprof"])}</td><td>{esc(row["node"])}</td>'
        f'<td class="num">{esc(fmt_seconds(row["old"]))}</td><td class="num">{esc(fmt_seconds(row["new"]))}</td><td class="num">{change_label(row)}</td></tr>'
        for row in change["evidence"]
    )
    return (
        f'<details><summary>{len(change["evidence"])} measured steps, median of {detect.WINDOW} runs on each side</summary><div class="scroll"><table>'
        f'<tr><th>Suite</th><th>Build</th><th>Host</th><th class="num">Before</th><th class="num">After</th><th class="num">Change</th></tr>{rows}</table></div></details>'
    )


def change_html(change):
    tags = []
    if change["control_moved"]:
        tags.append('<span class="tag">Likely host load: the Karma control moved too</span>')
    if change["tests_changed"]:
        tags.append('<span class="tag">Test count changed</span>')
    prs = "".join(
        f'<li><a href="https://github.com/civicrm/civicrm-core/pull/{esc(m["number"])}">#{esc(m["number"])}</a> {esc(m["title"])}'
        f'<br><span class="sub">Merged {esc(fmt_time(m["at"]))}, matches {m["fit"] * 100:.0f}% of the runs</span></li>'
        for m in change["candidates"]
    )
    more = f'<li class="sub">and {change["more_candidates"]} less likely</li>' if change["more_candidates"] else ""
    prs_block = f"<ul>{prs}{more}</ul>" if prs else '<p class="sub">No merge recorded in this window.</p>'
    return (
        f'<article class="change" id="change-{change["at"].strftime("%Y%m%d%H%M")}"><div><p class="delta">{change_title(change).capitalize()}</p>'
        f'<p class="when">around {esc(fmt_time(change["at"]))}</p>{"".join(tags)}</div>'
        f'<div><p>Seen in {change["series"]} series of {len(change["suites"])} {"suite" if len(change["suites"]) == 1 else "suites"} on {esc(", ".join(change["nodes"]))}, as the median change of these series. '
        f'Other jobs on the host, median: {change["parallel_before"]:g} before, {change["parallel_after"]:g} after.</p>'
        f'<p class="sub">Candidates merged between {esc(fmt_time(change["low"]))} and {esc(fmt_time(change["high"]))}, not established causes:</p>{prs_block}{evidence_table(change)}</div></article>'
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
    last_run = fmt_time(datetime.fromisoformat(runs[-1]["at"])) if runs else "never"
    recent = changes[:CHANGES_ON_OVERVIEW]
    older = f'<p class="sub">{len(changes) - len(recent)} older changes are on the month pages.</p>' if len(changes) > len(recent) else ""
    site = {"index.html": render_page("CiviCRM CI Stats", f"Last collected: {last_run}. ", jobs, recent, changes, older, month_nav(months, None))}
    for month in months:
        in_month = [change for change in changes if change["at"].strftime("%Y-%m") == month]
        site[f"{month}.html"] = render_page(f"CiviCRM CI Stats {month}", "", [job for job in jobs if month_of(job) == month], in_month, in_month, "", month_nav(months, month))
    return site


def host_filters(nodes, colors):
    boxes = "".join(
        f'<label><input type="checkbox" checked data-filter="{esc(node)}"><span class="key" style="background:{colors[node]}"></span>{esc(node)}</label>'
        for node in nodes
    )
    return f'<div class="filters" role="group" aria-label="Show hosts">Hosts {boxes}</div>'


def hero_chart(comparable, config, period, unit, marked, first, last):
    totals = medians_per(period, batch_totals(comparable, config))
    if not totals:
        return ""
    return line_chart(
        "Summed test time per batch", f"All PHPUnit suites of complete, successful PR batches, median per {unit}. Dashed lines mark detected changes.",
        {"Summed time": totals}, {"Summed time": "var(--s1)"}, marked, fmt_seconds, False, first, last, hero=True)


def host_of(job):
    return job["node"] or UNKNOWN_HOST


def host_charts(measured, config, period, unit, marked, first, last):
    """Return the host legend with every chart it scopes: one per suite and the load per host."""
    known = sorted({job["node"] for job in measured if job["node"]})
    colors = {node: f"var(--s{index + 1})" for index, node in enumerate(known[:SERIES_SLOTS])}
    nodes = known + ([UNKNOWN_HOST] if any(not job["node"] for job in measured) else [])
    for node in nodes[len(colors):]:
        colors[node] = "var(--text-2)"
    grid, charted = "", []
    for suite in sorted(config):
        selected = [job for job in measured if job["suite"] == suite and (job["bldtype"], job["bkprof"]) == config[suite]]
        charted += selected
        series = {node: medians_per(period, [(detect.parse_time(j["started"]).date(), float(j["duration"])) for j in selected if host_of(j) == node]) for node in nodes}
        series = {node: days for node, days in series.items() if days}
        grid += line_chart(suite, f"{config[suite][0]}, {config[suite][1]}, median per {unit} and host", series, colors, marked, fmt_seconds, True, first, last)
    load = defaultdict(list)
    for job in charted:
        if job.get("parallel") is not None:
            load[job["node"]].append((detect.parse_time(job["started"]).date(), job["parallel"]))
    load_chart = ""
    if load:
        load_chart = '<h2>Load per host</h2>' + line_chart(
            "Other jobs running beside the measured jobs", f"Median per {unit} over the jobs charted above, counting every batch type. Jobs outside Duderino are not visible.",
            {node: medians_per(period, rows) for node, rows in load.items()}, colors, marked, lambda v: f"{v:g}", True, first, last, axis=(COUNT_STEPS, lambda v: f"{v:g}"))
    return f'<section class="hosts">{host_filters(nodes, colors)}<h2>Per suite and host</h2><div class="grid">{grid}</div>{load_chart}</section>'


def render_page(heading, stamp, jobs, listed, marked, older, nav):
    """Render one page: listed changes as text, marked changes as dashed lines in the charts."""
    measured = [job for job in jobs if detect.is_measured(job)]
    changes_html = "".join(change_html(change) for change in listed)
    hero, charts = "", ""
    if measured:
        days_seen = [detect.parse_time(job["started"]).date() for job in measured]
        last = max(days_seen)
        period, unit = (week_start, "week") if (last - min(days_seen)).days > MAX_DAILY_DAYS else (lambda day: day, "day")
        first = period(min(days_seen))
        config = usual_config(measured)
        hero = hero_chart(measured, config, period, unit, marked, first, last)
        charts = host_charts(measured, config, period, unit, marked, first, last)
    without_host = any(job["node"] for job in measured) and not all(job["node"] for job in measured)
    coverage = f'<p class="sub">Jobs without a recorded host appear as "{UNKNOWN_HOST}" in the charts of their configuration and are not checked for changes.</p>' if without_host else ""
    changes_section = (
        f'<h2>Detected changes</h2><p class="sub">Successful PR runs against master only. Test time reflects the test harness as well as the code, '
        f"so a change here is a lead, not a measurement of CiviCRM's own speed. A change needs {detect.WINDOW} runs per host after it before it can appear.</p>{changes_html}{older}"
    ) if changes_html else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(heading)}</title><style>{STYLE}</style></head><body><div class="page">'
        f"<header><h1>{esc(heading)}</h1>{nav}</header>"
        f'<main><p class="lede">How long the civicrm-core pull request tests take on test.civicrm.org, and when that changes.</p>'
        f'<h2 class="headline">{esc(headline(listed, measured))}</h2>{coverage}{hero}{changes_section}{charts}</main>'
        f'<footer class="sub">{esc(stamp)}{len(measured)} measured jobs of {len(jobs)} collected. '
        f'<a href="https://github.com/civico-de/civicrm-ci-stats">Source and data</a></footer>'
        f'<div id="tip" role="status"></div></div><script>{SCRIPT}</script></body></html>\n'
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
