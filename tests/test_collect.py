import csv
import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

import collect

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
ALL_JOBS = (FIXTURES / "all-jobs.html").read_text()
BATCH = (FIXTURES / "batch.html").read_text()
TASK = (FIXTURES / "task-success.html").read_text()
GREEN = TASK.replace("phpunit-e2e phpunit-uf", "phpunit-api4")
PR_URL = f"{collect.BASE}/batch/CiviCRM-PR"
TOKEN = "test-token"


def batch_page(batch, pending=0, stamp="2026-11-08T15:17:06+00:00"):
    return (BATCH.replace("20893629", str(batch)).replace("Pending Jobs (0)", f"Pending Jobs ({pending})")
            .replace("2026-11-08T15:17:06+00:00", stamp))


def pulls(merged="2026-11-08T00:31:00Z"):
    return json.dumps([{"number": 37835, "merged_at": merged, "title": "Reuse container", "updated_at": "2026-01-01T00:00:00Z"},
                       {"number": 37000, "merged_at": None, "title": "Closed", "updated_at": "2026-01-01T00:00:00Z"}])


class Site:
    """Serves canned pages; every URL not listed is a 404."""

    def __init__(self, pages):
        self.pages = {f"{collect.BASE}/all-jobs": ALL_JOBS, **pages}
        self.requests = []

    def __call__(self, url, headers=None):
        self.requests.append(url)
        if url.startswith(collect.PULLS):
            return 200, pulls()
        return (200, self.pages[url]) if url in self.pages else (404, "")


def finished(batch):
    return {f"{PR_URL}/{batch}": batch_page(batch), f"{PR_URL}/{batch}/684100": GREEN, f"{PR_URL}/{batch}/684101": GREEN}


def rows(directory, name):
    with (directory / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


class Parsing(unittest.TestCase):
    def test_all_jobs_gives_newest_batch_and_types(self):
        self.assertEqual(collect.parse_all_jobs(ALL_JOBS), (20893632, ["Adhoc", "CiviCRM-PR"]))

    def test_batch_excludes_steps_links(self):
        info = collect.parse_batch(BATCH, "CiviCRM-PR", 20893629)
        self.assertEqual(info["tasks"], [684100, 684101])
        self.assertEqual((info["pending"], info["pr"]), (0, "37973"))

    def test_task_keeps_multi_word_suite_and_all_fields(self):
        params, details = collect.parse_task(TASK)
        self.assertEqual(params["SUITE"], "phpunit-e2e phpunit-uf")
        self.assertEqual(params["ORNERY"], "disable")
        self.assertEqual((details["duration"], details["node"]), (148.324, "test-3"))

    def test_batch_without_task_links_raises(self):
        with self.assertRaises(collect.ParseError):
            collect.parse_batch(BATCH.replace("/20893629/", "/renamed/"), "CiviCRM-PR", 20893629)

    def test_empty_last_parameter_is_empty(self):
        params, _ = collect.parse_task(TASK.replace("<td>disable</td>", "<td></td>"))
        self.assertEqual(params["ORNERY"], "")

    def test_failed_job_has_empty_duration_and_real_wall_time(self):
        page = TASK.replace('"duration": 148.324', '"duration": null').replace('"outcome": "SUCCESS"', '"outcome": "FAILURE"')
        row = collect.job_row("CiviCRM-PR", 1, 2, "3", *collect.parse_task(page))
        self.assertEqual((row["duration"], row["wall"], row["outcome"]), ("", 267, "FAILURE"))

    def test_changed_pages_raise(self):
        with self.assertRaises(collect.ParseError):
            collect.parse_task("<html>nothing</html>")
        with self.assertRaises(collect.ParseError):
            collect.parse_task(TASK.replace("Parameter", "Option"))
        with self.assertRaises(collect.ParseError):
            collect.parse_task(TASK.replace('"status": "FINALIZED", ', ""))
        with self.assertRaises(collect.ParseError):
            collect.parse_task(TASK.replace('"status"', '"status" oops'))
        with self.assertRaises(collect.ParseError):
            collect.parse_batch("<p>empty</p>", "CiviCRM-PR", 1)
        with self.assertRaises(collect.ParseError):
            collect.parse_all_jobs("<p>empty</p>")


class Running(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "data"
        self.data.mkdir()

    def go(self, site, start_id=None, max_batches=150, dry_run=False, token=TOKEN, **options):
        return collect.run(site, self.data, start_id, max_batches, dry_run, token, lambda: None, NOW, **options)

    def test_collects_across_gaps_and_tries_other_types(self):
        pages = {**finished(20893629), f"{collect.BASE}/batch/Adhoc/20893631": batch_page(20893631).replace("CiviCRM-PR", "Adhoc"),
                 **finished(20893632)}
        pages[f"{collect.BASE}/batch/Adhoc/20893631/684100"] = GREEN
        pages[f"{collect.BASE}/batch/Adhoc/20893631/684101"] = GREEN
        summary = self.go(Site(pages), start_id=20893629)
        self.assertEqual((summary["batches"], summary["jobs"], summary["last_id"]), (3, 6, 20893632))
        jobs = collect.read_jobs(self.data)
        self.assertEqual({job["batch_type"] for job in jobs}, {"CiviCRM-PR", "Adhoc"})
        self.assertEqual(jobs[0]["pr"], "37973")
        self.assertEqual((self.data / "cursor.txt").read_text(), "20893632\n")
        self.assertEqual(rows(self.data, "merges.csv"), [{"number": "37835", "merged_at": "2026-11-08T00:31:00Z", "title": "Reuse container"}])
        self.assertEqual(len(rows(self.data, "runs.csv")), 1)

    def test_second_run_starts_after_cursor_and_fetches_no_known_task(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        site = Site({})
        summary = self.go(site)
        self.assertEqual((summary["first_id"], summary["jobs"]), (20893633, 0))
        self.assertEqual(site.requests, [f"{collect.BASE}/all-jobs"])

    def test_back_collects_below_the_oldest_batch_and_keeps_the_cursor(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        summary = self.go(Site({**finished(20893630), **finished(20893629)}), back=True, max_gap=5)
        self.assertEqual((summary["mode"], summary["first_id"], summary["last_id"], summary["batches"]), ("back", 20893629, 20893630, 2))
        self.assertEqual((self.data / "cursor.txt").read_text(), "20893632\n")
        self.assertEqual({job["batch_id"] for job in collect.read_jobs(self.data)}, {"20893629", "20893630", "20893632"})

    def test_back_ends_at_a_long_gap_without_writing(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        before = (self.data / "runs.csv").read_text()
        site = Site({})
        self.assertEqual(self.go(site, back=True, max_gap=3)["batches"], 0)
        self.assertEqual(sum("/batch/CiviCRM-PR/" in url for url in site.requests), 3)
        self.assertEqual((self.data / "runs.csv").read_text(), before)

    def test_back_resumes_below_what_an_earlier_walk_stored(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        self.go(Site(finished(20893631)), back=True, max_batches=1)
        site = Site(finished(20893630))
        self.go(site, back=True, max_batches=1)
        self.assertFalse([url for url in site.requests if "20893631" in url or "20893632" in url])

    def test_back_keeps_collected_batches_when_a_page_breaks(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        summary = self.go(Site({**finished(20893631), f"{PR_URL}/20893630": "<p>changed</p>"}), back=True)
        self.assertEqual(summary["batches"], 1)
        self.assertIn("20893630", rows(self.data, "runs.csv")[-1]["error"])
        self.assertIn("20893631", {job["batch_id"] for job in collect.read_jobs(self.data)})

    def test_back_keeps_jobs_when_merges_cannot_be_read(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        site = Site(finished(20893631))
        failing = lambda url, headers=None: (500, "") if url.startswith(collect.PULLS) else site(url, headers)
        summary = self.go(failing, back=True, max_batches=1)
        self.assertIn("merges", summary["error"])
        self.assertIn("20893631", {job["batch_id"] for job in collect.read_jobs(self.data)})

    def test_a_later_walk_without_new_batches_repairs_missing_merges(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        (self.data / "merges.csv").unlink()
        summary = self.go(Site({}), back=True, max_gap=2)
        self.assertEqual((summary["batches"], summary["merges"]), (0, 1))
        self.assertEqual(rows(self.data, "merges.csv")[0]["number"], "37835")

    def test_invalid_pull_response_is_a_parse_error(self):
        with self.assertRaises(collect.ParseError):
            collect.fetch_merges(lambda url, headers=None: (200, "<html>"), TOKEN, NOW)

    def test_forward_writes_nothing_when_merges_cannot_be_read(self):
        site = Site(finished(20893632))
        with self.assertRaises(collect.ParseError):
            self.go(lambda url, headers=None: (500, "") if url.startswith(collect.PULLS) else site(url, headers), start_id=20893632)
        self.assertFalse((self.data / "jobs").exists())

    def test_back_walks_on_past_a_batch_without_finished_jobs(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        waiting = GREEN.replace("FINALIZED", "RUNNING")
        pages = {f"{PR_URL}/20893631": batch_page(20893631), f"{PR_URL}/20893631/684100": waiting, f"{PR_URL}/20893631/684101": waiting, **finished(20893630)}
        summary = self.go(Site(pages), back=True, max_batches=1)
        self.assertEqual((summary["batches"], summary["first_id"]), (1, 20893630))

    def test_known_batch_interrupts_a_run_of_missing_ids(self):
        self.go(Site(finished(20893631)), start_id=20893631)
        site = Site(finished(20893629))
        self.assertEqual(self.go(site, start_id=20893632, back=True, max_gap=2)["first_id"], 20893629)

    def test_back_fails_when_the_first_page_already_breaks(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        with self.assertRaises(collect.ParseError):
            self.go(Site({f"{PR_URL}/20893631": "<p>changed</p>"}), back=True)

    def test_jobs_are_stored_per_month(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        october = GREEN.replace("2026-11-08", "2026-10-31")
        self.go(Site({f"{PR_URL}/20893631": batch_page(20893631), f"{PR_URL}/20893631/684100": october, f"{PR_URL}/20893631/684101": october}), back=True, max_batches=1)
        self.assertEqual(sorted(path.name for path in (self.data / "jobs").iterdir()), ["2026-10.csv", "2026-11.csv"])
        self.assertEqual(len(collect.read_jobs(self.data)), 4)

    def test_job_without_queue_time_is_rejected(self):
        with self.assertRaises(collect.ParseError):
            collect.job_row("CiviCRM-PR", 1, 2, "", {}, {"queued": None})

    def test_run_without_news_writes_nothing(self):
        self.go(Site(finished(20893632)), start_id=20893632)
        snapshot = lambda: {path: path.read_bytes() for path in self.data.rglob("*") if path.is_file()}
        before = snapshot()
        self.go(Site({}))
        self.assertEqual(snapshot(), before)

    def test_running_batch_holds_cursor_back(self):
        pages = {**finished(20893630), f"{PR_URL}/20893631": batch_page(20893631, pending=2, stamp="2026-11-09T03:50:00+00:00"), **finished(20893632)}
        summary = self.go(Site(pages), start_id=20893630)
        self.assertEqual((summary["last_id"], summary["stopped_at_open"], summary["jobs"]), (20893630, 20893631, 2))

    def test_unfinished_task_holds_cursor_back_without_pending_jobs(self):
        pages = {**finished(20893631), **finished(20893632)}
        pages[f"{PR_URL}/20893632/684101"] = TASK.replace('"status": "FINALIZED"', '"status": "RUNNING"')
        summary = self.go(Site(pages), start_id=20893631)
        self.assertEqual((summary["last_id"], summary["stopped_at_open"], summary["jobs"]), (20893631, 20893632, 2))

    def test_stale_open_batch_does_not_block(self):
        pages = {**finished(20893632), f"{PR_URL}/20893631": batch_page(20893631, pending=1), f"{PR_URL}/20893631/684100": GREEN,
                 f"{PR_URL}/20893631/684101": TASK.replace('"status": "FINALIZED"', '"status": "RUNNING"')}
        pages[f"{PR_URL}/20893631"] = batch_page(20893631, pending=1, stamp="2026-11-01T00:00:00+00:00")
        summary = self.go(Site(pages), start_id=20893631)
        self.assertEqual((summary["abandoned"], summary["last_id"], summary["jobs"]), (1, 20893632, 3))

    def test_max_batches_stops_and_resumes(self):
        pages = {**finished(20893631), **finished(20893632)}
        self.assertEqual(self.go(Site(pages), start_id=20893631, max_batches=1)["last_id"], 20893631)
        self.assertEqual(self.go(Site(pages))["last_id"], 20893632)
        self.assertEqual(len(collect.read_jobs(self.data)), 4)

    def test_dry_run_writes_nothing(self):
        summary = self.go(Site(finished(20893632)), start_id=20893632, dry_run=True, token=None)
        self.assertEqual(summary["jobs"], 2)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_field_summary_shows_empty_fields_and_values(self):
        row = collect.job_row("CiviCRM-PR", 1, 2, "", *collect.parse_task(TASK))
        summary = collect.field_summary([row])
        self.assertEqual(summary["empty"], {"pr": 1})
        self.assertEqual(summary["suites"], {"phpunit-e2e phpunit-uf": 1})
        self.assertEqual(summary["example"]["node"], "test-3")
        self.assertIsNone(collect.field_summary([])["example"])

    def test_workers_keep_task_order_and_run_concurrently(self):
        class Slow(Site):
            active = peak = 0
            lock = threading.Lock()

            def __call__(self, url, headers=None):
                with self.lock:
                    Slow.active += 1
                    Slow.peak = max(Slow.peak, Slow.active)
                time.sleep(0.02)
                try:
                    return super().__call__(url, headers)
                finally:
                    with self.lock:
                        Slow.active -= 1

        collect.run(Slow(finished(20893632)), self.data, 20893632, 150, False, TOKEN, lambda: None, NOW, workers=2)
        self.assertEqual([job["task_id"] for job in collect.read_jobs(self.data)], ["684100", "684101"])
        self.assertEqual(Slow.peak, 2)

    def test_parse_error_writes_nothing(self):
        pages = {**finished(20893631), **finished(20893632)}
        pages[f"{PR_URL}/20893632/684101"] = "<html>redesigned</html>"
        with self.assertRaises(collect.ParseError):
            self.go(Site(pages), start_id=20893631)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_listed_task_missing_is_an_error(self):
        pages = finished(20893632)
        del pages[f"{PR_URL}/20893632/684101"]
        with self.assertRaises(collect.ParseError):
            self.go(Site(pages), start_id=20893632)

    def test_missing_start_id_and_token_refuse(self):
        with self.assertRaises(SystemExit):
            self.go(Site({}))
        with self.assertRaises(SystemExit):
            self.go(Site({}), start_id=1, token=None)

    def test_known_merge_is_not_appended_twice(self):
        self.go(Site(finished(20893631)), start_id=20893631)
        self.go(Site(finished(20893632)))
        self.assertEqual(len(rows(self.data, "merges.csv")), 1)

    def test_type_seen_earlier_is_still_tried(self):
        pages = {**finished(20893631), f"{collect.BASE}/batch/Nightly/20893632": batch_page(20893632).replace("CiviCRM-PR", "Nightly"),
                 f"{collect.BASE}/batch/Nightly/20893632/684100": GREEN, f"{collect.BASE}/batch/Nightly/20893632/684101": GREEN}
        self.go(Site(finished(20893630)), start_id=20893630, max_batches=1)
        path = next((self.data / "jobs").glob("*.csv"))
        path.write_text(path.read_text().replace("CiviCRM-PR", "Nightly", 1).replace("CiviCRM-PR", "Nightly"))
        self.go(Site(pages))
        self.assertIn("Nightly", {job["batch_type"] for job in collect.read_jobs(self.data) if job["batch_id"] == "20893632"})

    def test_merges_reach_back_to_the_last_known_job(self):
        self.go(Site(finished(20893630)), start_id=20893630, max_batches=1)
        calls = []
        real = collect.fetch_merges
        collect.fetch_merges = lambda fetch, token, since: calls.append(since) or []
        self.addCleanup(setattr, collect, "fetch_merges", real)
        later = GREEN.replace("2026-11-08 17:53:07", "2026-11-20 17:53:07").replace("2026-11-08 17:57:34", "2026-11-20 17:57:34")
        pages = {f"{PR_URL}/20893632": batch_page(20893632), f"{PR_URL}/20893632/684100": later, f"{PR_URL}/20893632/684101": later}
        self.go(Site(pages))
        self.assertEqual(calls[0].date().isoformat(), "2026-11-07")

    def test_pull_pages_are_limited(self):
        def endless(url, headers=None):
            return 200, json.dumps([{"number": 1, "merged_at": None, "title": "x", "updated_at": "2030-01-01T00:00:00Z"}])
        with self.assertRaises(collect.ParseError):
            collect.fetch_merges(endless, TOKEN, NOW)


if __name__ == "__main__":
    unittest.main()
