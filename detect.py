"""Find lasting steps in test durations that show up across suites and hosts."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

WINDOW = 8  # runs on each side of a candidate step
MIN_PRS = 3  # distinct PRs per window, so one slow PR cannot fake a step
MIN_SEPARATION = 0.85  # Cliff's delta between the two windows
MIN_CHANGE = 0.05  # relative change of the median
CLUSTER_GAP = timedelta(hours=8)  # events this close belong to one change
# Simulated healthy months gave 0 false alarms with these limits, 58 per 30 months with 3 series.
MIN_NODES, MIN_SERIES, MIN_NODES_ONE_SUITE = 2, 6, 3
TEST_COUNT_TOLERANCE = 0.05
FIT_MARGIN = 0.03  # candidates this close to the best fit stay listed


def parse_time(value):
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def is_control(suite):
    return "karma" in suite


def is_comparable(job):
    """Successful PR runs against master of suites whose runtime is meaningful."""
    return (
        job["batch_type"] == "CiviCRM-PR" and job["civiver"] == "master" and job["outcome"] == "SUCCESS"
        and job["ornery"] == "disable" and job["duration"] != "" and job["started"] != "" and job["node"] != ""
        and ("phpunit" in job["suite"] or is_control(job["suite"]))
    )


def tests_of(job):
    return sum(int(job[key] or 0) for key in ("passed", "failed", "skipped"))


def add_parallel(jobs):
    """Set job['parallel'] to the number of other jobs overlapping it on the same host."""
    by_node = defaultdict(list)
    for job in jobs:
        if job["node"] and job["started"] and job["completed"]:
            by_node[job["node"]].append(job)
    for group in by_node.values():
        starts = sorted(job["started"] for job in group)
        ends = sorted(job["completed"] for job in group)
        for job in group:
            job["parallel"] = bisect_left(starts, job["completed"]) - bisect_right(ends, job["started"]) - 1


def cliffs_delta(before, after):
    return sum((b > a) - (b < a) for a in before for b in after) / (len(before) * len(after))


def series_events(key, points):
    """Return the strongest candidate of every contiguous run of candidate positions."""
    events, current, last = [], None, None
    for index in range(WINDOW, len(points) - WINDOW + 1):
        before, after = points[index - WINDOW:index], points[index:index + WINDOW]
        if min(len({p["pr"] for p in before}), len({p["pr"] for p in after})) < MIN_PRS:
            continue
        old, new = [p["duration"] for p in before], [p["duration"] for p in after]
        delta, change = cliffs_delta(old, new), median(new) / median(old) - 1
        if abs(delta) < MIN_SEPARATION or abs(change) < MIN_CHANGE or (delta > 0) != (change > 0):
            continue
        count_old, count_new = median(p["tests"] for p in before), median(p["tests"] for p in after)
        event = {
            "suite": key[0], "bldtype": key[1], "bkprof": key[2], "node": key[3], "at": after[0]["at"],
            "last_before": before[-1]["at"], "first_after": after[0]["at"], "old": median(old), "new": median(new), "runs": before + after, "change": change, "score": abs(delta * change),
            "tests_changed": count_old > 0 and abs(count_new / count_old - 1) > TEST_COUNT_TOLERANCE,
            "parallel_before": median(p["parallel"] for p in before), "parallel_after": median(p["parallel"] for p in after),
        }
        if last is not None and index == last + 1:
            current = max(current, event, key=lambda item: item["score"])
            events[-1] = current
        else:
            current = event
            events.append(event)
        last = index
    return events


def split_clusters(events):
    """Group time-sorted events; a cluster ends at the first gap longer than CLUSTER_GAP."""
    clusters = []
    for event in events:
        if clusters and event["at"] - clusters[-1][-1]["at"] <= CLUSTER_GAP:
            clusters[-1].append(event)
        else:
            clusters.append([event])
    return clusters


def changes_from_events(events, merges):
    """Merge per-series events into changes that hold across several suites and hosts."""
    changes = []
    for direction in (-1, 1):
        same = sorted((e for e in events if (e["change"] > 0) == (direction > 0)), key=lambda e: e["at"])
        for group in split_clusters(same):
            real = drop_lone_suites([e for e in group if not is_control(e["suite"])])
            suites, nodes = {e["suite"] for e in real}, {e["node"] for e in real}
            if is_broad(real, suites, nodes):
                changes.append(build_change(group, real, suites, nodes, merges))
    return sorted(changes, key=lambda change: change["at"], reverse=True)


def series_count(events):
    return len({(e["suite"], e["bldtype"], e["bkprof"], e["node"]) for e in events})


def drop_lone_suites(real):
    """In a small group, a suite seen on one host only is chance rather than part of the change."""
    if series_count(real) >= MIN_SERIES:
        return real
    return [e for e in real if len({o["node"] for o in real if o["suite"] == e["suite"]}) >= 2]


def is_broad(real, suites, nodes):
    """A change spans several hosts: either many series, or one suite on most hosts."""
    widest = max((len({e["node"] for e in real if e["suite"] == suite}) for suite in suites), default=0)
    return len(nodes) >= MIN_NODES and (series_count(real) >= MIN_SERIES or widest >= MIN_NODES_ONE_SUITE)


def fit_of(merge, runs):
    """Share of runs matching the old level before this merge and the new level after it."""
    number = str(merge["number"])
    return sum((run["at"] >= merge["at"] or run["pr"] == number) == is_new for run, is_new in runs) / len(runs)


def rank_candidates(candidates, real):
    runs = [(run, abs(run["duration"] - e["new"]) < abs(run["duration"] - e["old"])) for e in real for run in e["runs"]]
    return sorted(({**m, "fit": fit_of(m, runs)} for m in candidates), key=lambda m: (-m["fit"], m["at"]))


def build_change(group, real, suites, nodes, merges):
    low, high = min(e["last_before"] for e in real), max(e["first_after"] for e in real)
    moments = sorted(e["at"] for e in real)
    middle = moments[len(moments) // 2]
    candidates = rank_candidates([m for m in merges if low <= m["at"] <= high], real)
    plausible = [m for m in candidates if m["fit"] >= candidates[0]["fit"] - FIT_MARGIN]
    return {
        "at": middle, "low": low, "high": high, "change": median(e["change"] for e in real),
        "suites": sorted(suites), "nodes": sorted(nodes), "series": series_count(real),
        "control_moved": len({e["node"] for e in group if is_control(e["suite"])}) >= MIN_NODES,
        "tests_changed": sum(e["tests_changed"] for e in real) * 2 >= len(real),
        "parallel_before": median(e["parallel_before"] for e in real),
        "parallel_after": median(e["parallel_after"] for e in real),
        "candidates": plausible, "more_candidates": len(candidates) - len(plausible),
    }


def find_changes(jobs, merges):
    """Return detected changes, newest first; jobs may be any rows from data/jobs.csv."""
    add_parallel(jobs)
    series = defaultdict(list)
    for job in jobs:
        if is_comparable(job):
            key = (job["suite"], job["bldtype"], job["bkprof"], job["node"])
            series[key].append({
                "at": parse_time(job["started"]), "duration": float(job["duration"]),
                "pr": job["pr"] or f"batch-{job['batch_id']}", "tests": tests_of(job), "parallel": job.get("parallel", 0),
            })
    events = []
    for key, points in series.items():
        events += series_events(key, sorted(points, key=lambda point: point["at"]))
    parsed = [{**m, "at": datetime.fromisoformat(m["merged_at"])} for m in merges]
    return changes_from_events(events, parsed)
