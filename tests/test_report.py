import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
import report
from tests import simulate

STEP = simulate.START + timedelta(days=20)
TODAY = (simulate.START + timedelta(days=30)).date()
MERGE = {"number": "37835", "merged_at": STEP.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "<b>Reuse</b> container"}


def effect(suite, node, moment, pr):
    return 0.68 if moment >= STEP and "karma" not in suite else 1


class Render(unittest.TestCase):
    def test_change_is_listed_and_title_escaped(self):
        page = report.render(simulate.jobs(1, effect=effect), [MERGE], [{"at": "2026-10-01T04:00:00+00:00"}], TODAY)
        self.assertRegex(page, r"3[0-4]% faster")
        self.assertIn("&lt;b&gt;Reuse&lt;/b&gt;", page)
        self.assertNotIn("<b>Reuse</b>", page)
        self.assertIn('data-node="test-3"', page)
        self.assertIn("Summed test time", page)
        self.assertNotIn("Likely host load", page)

    def test_healthy_data_lists_nothing(self):
        page = report.render(simulate.jobs(2), [], [], TODAY)
        self.assertIn("No lasting change detected", page)

    def test_empty_data_renders(self):
        page = report.render([], [], [], TODAY)
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
            data.mkdir()
            rows = simulate.jobs(3, days=3)
            with (data / "jobs.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            out = Path(temp) / "site" / "index.html"
            subprocess.run([sys.executable, str(ROOT / "report.py"), "--data", str(data), "--out", str(out)], check=True)
            self.assertIn("CiviCRM CI Stats", out.read_text())


if __name__ == "__main__":
    unittest.main()
