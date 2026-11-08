#!/usr/bin/env python3
"""Append finished Duderino test jobs and merged civicrm-core PRs to data/."""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = "https://test.civicrm.org/duderino"
PULLS = "https://api.github.com/repos/civicrm/civicrm-core/pulls"
USER_AGENT = "civicrm-ci-stats"
PAUSE_SECONDS = 1.0
OPEN_BATCH_MAX_AGE = timedelta(days=3)
MAX_PULL_PAGES = 30
PARAMS = ("_job", "CIVIVER", "BLDTYPE", "BKPROF", "SUITE", "PATCH", "ORNERY")

JOB_FIELDS = [
    "batch_type", "batch_id", "task_id", "civiver", "pr", "bldtype", "bkprof", "suite", "ornery",
    "jenkins_job", "build_id", "outcome", "node", "image", "queued", "started", "completed",
    "duration", "wall", "passed", "failed", "skipped",
]
MERGE_FIELDS = ["number", "merged_at", "title"]
RUN_FIELDS = ["at", "first_id", "last_id", "batches", "jobs", "merges", "stopped_at_open", "abandoned"]


class ParseError(Exception):
    """A Duderino page no longer has the structure this collector relies on."""


def text_of(page):
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def parse_all_jobs(page):
    """Return the newest batch id and the batch types listed on the all-jobs page."""
    found = re.findall(r"/duderino/batch/([\w.-]+)/(\d+)/\d+[\"']", page)
    if not found:
        raise ParseError("all-jobs: no batch links found")
    return max(int(batch) for _, batch in found), sorted({kind for kind, _ in found})


def parse_batch(page, kind, batch):
    """Return task ids, pending count, latest timestamp and PR of a batch page."""
    pattern = rf"/duderino/batch/{re.escape(kind)}/{batch}/(\d+)[\"']"
    tasks = sorted({int(task) for task in re.findall(pattern, page)})
    pending = re.search(r"Pending Jobs \((\d+)\)", text_of(page))
    stamps = re.findall(r"<ui-timestamp[^>]*\bvalue=[\"']([^\"']+)", page)
    if pending is None or not stamps or not tasks:
        raise ParseError(f"batch {kind}/{batch}: pending count, timestamp or task links not found")
    pr = re.search(r"civicrm-core/pull/(\d+)", page)
    latest = max(datetime.fromisoformat(stamp) for stamp in stamps)
    return {"tasks": tasks, "pending": int(pending.group(1)), "latest": latest, "pr": pr.group(1) if pr else ""}


def parse_params(page):
    text = text_of(page)
    match = re.search(r"Parameter Value (.*?)(?:Jenkins Build|$)", text)
    if match is None:
        raise ParseError("task: parameter table not found")
    parts = re.split(rf"(?:^|\s)({'|'.join(PARAMS)})(?=\s|$)", match.group(1).strip())
    return {key: value.strip() for key, value in zip(parts[1::2], parts[2::2])}


def parse_task(page):
    """Return the task's parameters and the Jenkins detail record."""
    match = re.search(r'\{\s*"jenkinsJob".*?\}', html.unescape(page), flags=re.DOTALL)
    if match is None:
        raise ParseError("task: Jenkins detail record not found")
    try:
        details = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ParseError(f"task: Jenkins detail record is not JSON: {error}") from error
    if "status" not in details:
        raise ParseError("task: Jenkins detail record has no status")
    return parse_params(page), details


def parse_time(value):
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def job_row(kind, batch, task, pr, params, details):
    get = lambda key: "" if details.get(key) is None else details[key]
    wall = ""
    if details.get("started") and details.get("completed"):
        wall = int((parse_time(details["completed"]) - parse_time(details["started"])).total_seconds())
    return {
        "batch_type": kind, "batch_id": batch, "task_id": task, "civiver": params.get("CIVIVER", ""), "pr": pr,
        "bldtype": params.get("BLDTYPE", ""), "bkprof": params.get("BKPROF", ""), "suite": params.get("SUITE", ""),
        "ornery": params.get("ORNERY", ""), "jenkins_job": get("jenkinsJob"), "build_id": get("jenkinsBuildId"),
        "outcome": get("outcome"), "node": get("node"), "image": get("image"), "queued": get("queued"),
        "started": get("started"), "completed": get("completed"), "duration": get("duration"), "wall": wall,
        "passed": get("passCount"), "failed": get("failCount"), "skipped": get("skipCount"),
    }


def http_get(url, headers=None):
    """Return (status, body); a 404 is an answer, other HTTP errors raise."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, ""
        raise


def fetch_batch(fetch, kind, batch, pause):
    status, page = fetch(f"{BASE}/batch/{kind}/{batch}")
    pause()
    return None if status == 404 else parse_batch(page, kind, batch)


def collect_batch(fetch, kind, batch, info, pause, workers):
    def load(task):
        status, page = fetch(f"{BASE}/batch/{kind}/{batch}/{task}")
        pause()
        return task, status, page

    with ThreadPoolExecutor(workers) as pool:
        pages = list(pool.map(load, info["tasks"]))
    rows, unfinished = [], 0
    for task, status, page in pages:
        if status == 404:
            raise ParseError(f"batch {kind}/{batch}: listed task {task} returns 404")
        params, details = parse_task(page)
        if details["status"] != "FINALIZED":
            print(f"note: {kind}/{batch}/{task} is {details['status']}", file=sys.stderr)
            unfinished += 1
            continue
        rows.append(job_row(kind, batch, task, info["pr"], params, details))
    return rows, unfinished


def walk(fetch, cursor, known, seen_kinds, max_batches, now, pause, workers):
    """Collect batches after the cursor; stop at the first batch that is still running."""
    status, page = fetch(f"{BASE}/all-jobs")
    pause()
    if status != 200:
        raise ParseError(f"all-jobs: HTTP {status}")
    head, listed = parse_all_jobs(page)
    kinds = ["CiviCRM-PR"] + sorted((set(listed) | seen_kinds) - {"CiviCRM-PR"})
    result = {"rows": [], "batches": 0, "cursor": cursor, "stopped_at_open": "", "abandoned": 0}
    for batch in range(cursor + 1, head + 1):
        if batch in known:
            result["cursor"] = batch
            continue
        if result["batches"] >= max_batches:
            break
        found = None
        for kind in kinds:
            found = fetch_batch(fetch, kind, batch, pause)
            if found is not None:
                break
        if found is not None:
            fresh = now - found["latest"] < OPEN_BATCH_MAX_AGE
            if found["pending"] and fresh:
                result["stopped_at_open"] = batch
                break
            rows, unfinished = collect_batch(fetch, kind, batch, found, pause, workers)
            if unfinished and fresh:
                result["stopped_at_open"] = batch
                break
            result["abandoned"] += 1 if found["pending"] or unfinished else 0
            result["rows"] += rows
            result["batches"] += 1
            print(f"batch {batch}: {len(rows)} jobs, {result['batches']} batches so far", file=sys.stderr, flush=True)
        result["cursor"] = batch
    return result


def fetch_merges(fetch, token, since):
    """Return merged master PRs updated since the given time, newest pages first."""
    merges = {}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    for number in range(1, MAX_PULL_PAGES + 1):
        query = f"state=closed&base=master&sort=updated&direction=desc&per_page=100&page={number}"
        status, body = fetch(f"{PULLS}?{query}", headers)
        if status != 200:
            raise ParseError(f"pulls: HTTP {status}")
        pulls = json.loads(body)
        for pull in pulls:
            if pull["merged_at"]:
                merges[pull["number"]] = {"number": pull["number"], "merged_at": pull["merged_at"], "title": pull["title"]}
        if not pulls or datetime.fromisoformat(pulls[-1]["updated_at"]) < since:
            return list(merges.values())
    raise ParseError(f"pulls: more than {MAX_PULL_PAGES} pages since {since.isoformat()}")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def append_csv(path, fields, rows):
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def field_summary(rows):
    """Show what the parsed rows contain, so a dry run proves more than the row count."""
    return {
        "empty": {field: empty for field in JOB_FIELDS if (empty := sum(row[field] == "" for row in rows))},
        "types": dict(Counter(row["batch_type"] for row in rows)),
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "versions": dict(Counter(row["civiver"] for row in rows)),
        "nodes": dict(Counter(row["node"] for row in rows)),
        "suites": dict(Counter(row["suite"] for row in rows)),
        "example": rows[0] if rows else None,
    }


def run(fetch, data_dir, start_id, max_batches, dry_run, token, pause, now, workers=1):
    """Collect everything first; any parse error leaves data/ untouched."""
    cursor_file = data_dir / "cursor.txt"
    if start_id is None and not cursor_file.exists():
        raise SystemExit("error: no data/cursor.txt yet, pass --start-id with the first batch id to collect")
    if token is None and not dry_run:
        raise SystemExit("error: GITHUB_TOKEN is required to read merged pull requests")
    cursor = start_id - 1 if start_id is not None else int(cursor_file.read_text(encoding="utf-8"))
    jobs = read_csv(data_dir / "jobs.csv")
    known = {int(job["batch_id"]) for job in jobs}
    result = walk(fetch, cursor, known, {job["batch_type"] for job in jobs}, max_batches, now, pause, workers)

    new_merges = []
    started = [parse_time(row["started"]) for row in result["rows"] if row["started"]]
    if started and not dry_run:
        have = {merge["number"] for merge in read_csv(data_dir / "merges.csv")}
        previous = [parse_time(job["started"]) for job in jobs if job["started"]]
        since = min(started + ([max(previous)] if previous else [])) - timedelta(days=1)
        new_merges = [merge for merge in fetch_merges(fetch, token, since) if str(merge["number"]) not in have]

    summary = {
        "at": now.isoformat(timespec="seconds"), "first_id": cursor + 1, "last_id": result["cursor"],
        "batches": result["batches"], "jobs": len(result["rows"]), "merges": len(new_merges),
        "stopped_at_open": result["stopped_at_open"], "abandoned": result["abandoned"],
    }
    print(("dry run: " if dry_run else "") + json.dumps(summary))
    if dry_run:
        print(json.dumps(field_summary(result["rows"]), indent=1))
        return summary
    if result["cursor"] == cursor and not result["rows"]:
        return summary
    data_dir.mkdir(parents=True, exist_ok=True)
    append_csv(data_dir / "jobs.csv", JOB_FIELDS, result["rows"])
    append_csv(data_dir / "merges.csv", MERGE_FIELDS, sorted(new_merges, key=lambda merge: merge["number"]))
    append_csv(data_dir / "runs.csv", RUN_FIELDS, [summary])
    cursor_file.write_text(f"{result['cursor']}\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--start-id", type=int, help="first batch id, required until data/cursor.txt exists")
    parser.add_argument("--max-batches", type=int, default=60, help="batches per run (19 to 49 requests each)")
    parser.add_argument("--workers", type=int, default=1, help="parallel task requests, for one-off backfills")
    parser.add_argument("--dry-run", action="store_true", help="print the counters and write nothing")
    args = parser.parse_args()
    try:
        run(http_get, args.data, args.start_id, args.max_batches, args.dry_run, os.environ.get("GITHUB_TOKEN"),
            lambda: time.sleep(PAUSE_SECONDS), datetime.now(UTC), args.workers)
    except (ParseError, urllib.error.URLError) as error:
        print(f"error: {error}; nothing was written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
