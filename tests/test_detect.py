import unittest
from datetime import UTC, datetime, timedelta

import detect
from tests import simulate

STEP = simulate.START + timedelta(days=20, hours=0, minutes=31)
MERGE = [{"number": "37835", "merged_at": STEP.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "Reuse container"},
         {"number": "1", "merged_at": "2026-09-02T00:00:00Z", "title": "Unrelated"}]


def after_step(multiplier, skip=()):
    def effect(suite, node, moment, pr):
        return multiplier if moment >= STEP and suite not in skip else 1
    return effect


class HealthySeries(unittest.TestCase):
    def test_healthy_month_produces_no_change_across_seeds(self):
        for seed in range(40):
            self.assertEqual(detect.find_changes(simulate.jobs(seed), []), [], f"false alarm with seed {seed}")

    def test_one_slow_pr_produces_no_change(self):
        def slow_pr(suite, node, moment, pr):
            return 2.0 if pr == "30010" else 1
        self.assertEqual(detect.find_changes(simulate.jobs(1, effect=slow_pr), []), [])

    def test_one_slow_host_produces_no_change(self):
        def slow_host(suite, node, moment, pr):
            return 1.6 if node == "test-3" and moment >= STEP else 1
        self.assertEqual(detect.find_changes(simulate.jobs(2, effect=slow_host), []), [])

    def test_one_suite_on_two_of_three_hosts_produces_no_change(self):
        def partial(suite, node, moment, pr):
            return 1.5 if suite == "phpunit-civi" and node != "test-5" and moment >= STEP else 1
        self.assertEqual(detect.find_changes(simulate.jobs(3, effect=partial), []), [])

    def test_failed_and_other_runs_are_ignored(self):
        rows = simulate.jobs(4, effect=after_step(0.5))
        for row in rows:
            row["outcome"] = "FAILURE"
        self.assertEqual(detect.find_changes(rows, []), [])
        rows = simulate.jobs(4, effect=after_step(0.5))
        for row in rows:
            row["civiver"] = "6.21"
        self.assertEqual(detect.find_changes(rows, []), [])


class Steps(unittest.TestCase):
    def assert_found(self, changes, change, tolerance=0.06):
        self.assertEqual(len(changes), 1, changes)
        self.assertAlmostEqual(changes[0]["change"], change, delta=tolerance)
        self.assertLess(abs(changes[0]["at"].replace(tzinfo=None) - STEP), timedelta(hours=12))

    def test_merged_speedup_is_found_with_its_pr_as_candidate(self):
        for seed in range(10):
            changes = detect.find_changes(simulate.jobs(seed, effect=after_step(0.68, skip={"mixin karma"})), MERGE)
            self.assert_found(changes, -0.32)
            self.assertEqual(changes[0]["nodes"], sorted(simulate.NODES))
            self.assertFalse(changes[0]["control_moved"])
            self.assertEqual([m["number"] for m in changes[0]["candidates"]], ["37835"])

    def test_one_suite_on_every_host_is_found(self):
        def slow_suite(suite, node, moment, pr):
            return 1.5 if suite == "phpunit-civi" and moment >= STEP else 1
        changes = detect.find_changes(simulate.jobs(3, effect=slow_suite), [])
        self.assert_found(changes, 0.5, tolerance=0.1)
        self.assertEqual(changes[0]["suites"], ["phpunit-civi"])

    def test_slowdown_is_found(self):
        self.assert_found(detect.find_changes(simulate.jobs(5, effect=after_step(1.3, skip={"mixin karma"})), []), 0.3)

    def test_host_wide_step_is_flagged_as_load(self):
        changes = detect.find_changes(simulate.jobs(6, effect=after_step(0.7)), [])
        self.assert_found(changes, -0.3)
        self.assertTrue(changes[0]["control_moved"])

    def test_test_count_change_is_flagged(self):
        rows = simulate.jobs(7, effect=after_step(1.3, skip={"mixin karma"}))
        for row in rows:
            if datetime.fromisoformat(row["started"]) >= STEP:
                row["passed"] = "2600"
        changes = detect.find_changes(rows, [])
        self.assert_found(changes, 0.3)
        self.assertTrue(changes[0]["tests_changed"])


class Qualifying(unittest.TestCase):
    def event(self, suite, node, hours):
        moment = simulate.START.replace(tzinfo=UTC) + timedelta(hours=hours)
        return {"suite": suite, "bldtype": "b", "bkprof": "p", "node": node, "at": moment, "last_before": moment, "first_after": moment,
                "change": -0.2, "score": 0.2, "tests_changed": False, "parallel_before": 1, "parallel_after": 1, "old": 100, "new": 80, "runs": []}

    def test_repeated_events_of_one_series_count_once(self):
        events = [self.event("a", "n1", hour) for hour in range(5)] + [self.event("a", "n2", 5)]
        self.assertEqual(detect.changes_from_events(events, []), [])

    def point(self, hours, duration, pr="1"):
        return {"at": simulate.START.replace(tzinfo=UTC) + timedelta(hours=hours), "duration": duration, "pr": pr}

    def merge(self, number, hours):
        return {"number": number, "at": simulate.START.replace(tzinfo=UTC) + timedelta(hours=hours)}

    def test_merge_at_the_step_ranks_first_and_distant_merges_drop_out(self):
        runs = [self.point(hour, 200) for hour in range(10)] + [self.point(hour, 100) for hour in range(10, 20)]
        event = {**self.event("a", "n1", 10), "old": 200, "new": 100, "runs": runs}
        ranked = detect.rank_candidates([self.merge("late", 17), self.merge("real", 9.5), self.merge("early", 2)], [event])
        self.assertEqual([m["number"] for m in ranked], ["real", "late", "early"])
        self.assertEqual(ranked[0]["fit"], 1.0)
        self.assertLess(ranked[1]["fit"], 1 - detect.FIT_MARGIN)

    def test_own_run_of_a_pr_counts_as_after_its_merge(self):
        runs = [self.point(1, 200), self.point(2, 100, pr="37"), self.point(3, 200), self.point(4, 100), self.point(5, 100)]
        event = {**self.event("a", "n1", 4), "old": 200, "new": 100, "runs": runs}
        self.assertEqual(detect.fit_of(self.merge("37", 3.5), [(r, abs(r["duration"] - 100) < abs(r["duration"] - 200)) for r in event["runs"]]), 1.0)
        self.assertEqual(detect.rank_candidates([self.merge("99", 3.5), self.merge("37", 3.5)], [event])[0]["number"], "37")

    def test_every_equally_fitting_merge_is_listed(self):
        runs = [self.point(hour, 200) for hour in range(10)] + [self.point(hour, 100) for hour in range(10, 20)]
        real = [{**self.event("a", "n1", 10), "last_before": runs[9]["at"], "first_after": runs[10]["at"], "old": 200, "new": 100, "runs": runs}]
        change = detect.build_change(real, real, {"a"}, {"n1"}, [self.merge(str(number), 9.5) for number in range(100, 112)])
        self.assertEqual((len(change["candidates"]), change["more_candidates"]), (12, 0))

    def test_six_distinct_series_qualify(self):
        events = [self.event(f"s{index}", f"n{index % 2}", index) for index in range(6)]
        changes = detect.changes_from_events(events, [])
        self.assertEqual((len(changes), changes[0]["series"]), (1, 6))


class Parallel(unittest.TestCase):
    def test_counts_only_overlaps_on_the_same_host(self):
        def job(node, start, end):
            return {"node": node, "started": f"2026-01-01 00:{start:02d}:00", "completed": f"2026-01-01 00:{end:02d}:00"}
        rows = [job("a", 0, 10), job("a", 5, 15), job("a", 10, 20), job("b", 0, 20)]
        detect.add_parallel(rows)
        self.assertEqual([row["parallel"] for row in rows], [1, 2, 1, 0])

    def test_job_without_times_is_skipped(self):
        rows = [{"node": "a", "started": "", "completed": ""}]
        detect.add_parallel(rows)
        self.assertNotIn("parallel", rows[0])


if __name__ == "__main__":
    unittest.main()
