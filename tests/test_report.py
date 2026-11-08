import csv
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
import detect
import report
from tests import simulate

STEP = simulate.START + timedelta(days=20)
MERGE = {"number": "37835", "merged_at": STEP.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "<b>Reuse</b> container"}


def effect(suite, node, moment, pr):
    return 0.68 if moment >= STEP and "karma" not in suite else 1


def overview(jobs, merges, runs):
    return report.render_site(jobs, merges, runs)["index.html"]


class Render(unittest.TestCase):
    def test_every_month_gets_a_page_with_its_own_change_and_daily_points(self):
        site = report.render_site(simulate.jobs(1, days=130, effect=effect), [MERGE], [])
        month = STEP.strftime("%Y-%m")
        self.assertIn(f"{month}.html", site)
        self.assertIn('class="delta">Down', site[f"{month}.html"])
        self.assertIn("median per day and host", site[f"{month}.html"])
        other = next(name for name in site if name not in ("index.html", f"{month}.html"))
        self.assertNotIn('class="delta"', site[other])
        self.assertIn(f'<a href="{month}.html" aria-current="page">', site[f"{month}.html"])
        self.assertIn('<a href="index.html" aria-current="page">', site["index.html"])

    def test_overview_limits_the_change_list_and_points_to_the_months(self):
        jobs, changes = [], [{"at": STEP, "change": -0.3, "suites": ["a", "b"]}] * 3
        original = report.CHANGES_ON_OVERVIEW, report.detect.find_changes, report.change_html
        self.addCleanup(lambda: (setattr(report, "CHANGES_ON_OVERVIEW", original[0]), setattr(report.detect, "find_changes", original[1]), setattr(report, "change_html", original[2])))
        report.CHANGES_ON_OVERVIEW = 2
        report.detect.find_changes = lambda *_: changes
        report.change_html = lambda change: "<p>one change</p>"
        page = overview(jobs, [], [])
        self.assertEqual(page.count("one change"), 2)
        self.assertIn("1 older changes", page)

    def test_change_is_listed_and_title_escaped(self):
        page = overview(simulate.jobs(1, effect=effect), [MERGE], [{"at": "2026-10-01T04:00:00+00:00"}])
        self.assertRegex(page, r'class="delta">Down 3[0-4]%')
        self.assertIn("&lt;b&gt;Reuse&lt;/b&gt;", page)
        self.assertNotIn("<b>Reuse</b>", page)
        self.assertIn('data-node="test-3"', page)
        self.assertIn("Summed test time", page)
        self.assertNotIn("Likely host load", page)

    def test_change_headline_matches_the_list_and_names_no_figure_on_the_sum(self):
        page = overview(simulate.jobs(1, effect=effect), [MERGE], [])
        percent = re.search(r'class="delta">Down (\d+)%', page).group(1)
        self.assertRegex(page, rf"Latest change: test time down {percent}% in \d+ suites")
        self.assertRegex(page, r"\d+ measured steps")
        hero = re.search(r"<svg data-chart.*?</svg>", page, re.DOTALL).group(0)
        self.assertNotRegex(re.sub(r'data-tip="[^"]*"', "", hero), r"\d%")

    def test_change_in_one_suite_is_named(self):
        change = {"at": datetime(2026, 11, 1, 12, tzinfo=UTC), "change": 2.19, "suites": ["phpunit-api4"]}
        self.assertEqual(report.headline([change]), "Latest change: test time up 219% in phpunit-api4 around 2026-11-01")

    def test_every_drawn_point_has_a_tooltip_row_at_its_own_date(self):
        page = overview(simulate.jobs(1, days=5), [], [])
        charts = re.findall(r"<svg data-chart.*?</svg>", page, re.DOTALL)
        self.assertEqual(len(re.findall(r'class="dot"', charts[0])), len(re.findall(r'class="col"', charts[0])))
        for chart in charts[1:]:
            columns = re.findall(r'data-x="([\d.]+)" data-tip="([^"]*)"', chart)
            groups = re.findall(r'<g data-node="([^"]+)">(.*?)</g>', chart, re.DOTALL)
            self.assertTrue(groups)
            for node, drawing in groups:
                self.assertEqual({x for x, tip in columns if f"{node}\t" in tip}, set(re.findall(r'cx="([\d.]+)"', drawing)), node)

    def test_chart_of_one_day_shows_only_that_day(self):
        day = simulate.START.strftime("%Y-%m-%d")
        page = overview([job for job in simulate.jobs(1, days=3) if job["started"][:10] == day], [], [])
        chart = re.findall(r"<svg data-chart.*?</svg>", page, re.DOTALL)[1]
        self.assertEqual(re.findall(r'y="214" text-anchor="\w+">([A-Z][a-z]+ \d+)', chart), [simulate.START.strftime("%b %d")])

    def test_healthy_data_lists_nothing(self):
        page = overview(simulate.jobs(2), [], [])
        self.assertIn("No lasting change detected", page)

    def test_long_history_is_charted_per_week_and_kept_whole(self):
        short, long = overview(simulate.jobs(4, days=30), [], []), overview(simulate.jobs(4, days=130), [], [])
        self.assertIn("median per day and host", short)
        self.assertIn("median per week and host", long)
        self.assertIn(f'data-day="{(simulate.START.date() - timedelta(days=simulate.START.weekday())).isoformat()}"', long)

    def test_first_week_stays_inside_the_chart_when_the_history_starts_midweek(self):
        monday = (simulate.START + timedelta(days=7 - simulate.START.weekday())).date()
        friday = (monday + timedelta(days=4)).isoformat()
        page = overview([job for job in simulate.jobs(4, days=140) if job["started"][:10] >= friday], [], [])
        self.assertIn("median per week", page)
        self.assertGreaterEqual(min(float(value) for value in re.findall(r'cx="([\d.-]+)"', page)), report.LEFT)

    def test_load_chart_covers_the_measured_jobs_only(self):
        measured = simulate.jobs(1, days=3)
        failed = [{**job, "task_id": f"f{index}", "outcome": "FAILURE", "duration": ""} for index, job in enumerate(measured)]
        page = overview(measured + failed, [], [])
        counts = re.findall(r"\(n=(\d+)\)", page[page.index("Load per host"):])
        self.assertEqual(sum(int(count) for count in counts), len([job for job in measured if detect.is_comparable(job)]))

    def test_load_chart_leaves_out_configurations_the_suite_charts_do_not_show(self):
        measured = simulate.jobs(1, days=3)
        usual = [job for job in measured if detect.is_comparable(job)]
        other = [{**job, "task_id": f"o{index}", "bkprof": "php00m00"} for index, job in enumerate(usual[:3])]
        page = overview(measured + other, [], [])
        counts = re.findall(r"\(n=(\d+)\)", page[page.index("Load per host"):])
        self.assertEqual(sum(int(count) for count in counts), len(usual))

    def test_empty_data_renders(self):
        page = overview([], [], [])
        self.assertIn("No lasting change detected", page)
        self.assertIn("never", page)

    def test_axis_ticks_are_round_and_reach_the_data(self):
        self.assertEqual(report.tick_values(5768, report.MINUTE_STEPS), [0, 1800, 3600, 5400, 7200])
        self.assertEqual(report.tick_values(4.5, report.COUNT_STEPS), [0, 2, 4, 6])
        self.assertEqual(report.tick_values(0, report.COUNT_STEPS), [0, 1])
        self.assertEqual(report.fmt_tick(5400), "1h 30m")
        self.assertEqual(report.fmt_tick(900), "15m")

    def test_format(self):
        self.assertEqual(report.fmt_seconds(816), "13m 36s")
        self.assertEqual(report.fmt_seconds(5768), "1h 36m")

    def test_cli_writes_the_page(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            (data / "jobs").mkdir(parents=True)
            rows = simulate.jobs(3, days=3)
            with (data / "jobs" / "1.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            out = Path(temp) / "site"
            subprocess.run([sys.executable, str(ROOT / "report.py"), "--data", str(data), "--out", str(out)], check=True)
            self.assertIn("CiviCRM CI Stats", (out / "index.html").read_text())
            self.assertEqual(len(list(out.glob("*.html"))), 1 + len({row["started"][:7] for row in rows if row["started"]}))


if __name__ == "__main__":
    unittest.main()
