"""整批交换精确置换检验、断点续算与缓存沿用的单元测试。"""
import itertools
import json
import os
import tempfile
import unittest
from fractions import Fraction

os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://u:p@127.0.0.1/db')
os.environ.setdefault('JWT_SECRET', 'unit-test')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp(prefix='analysis-test-'))

from src.domain import analysis
from src.domain.analysis import analyze, batch_fingerprint
from src.domain.demo import demo_rows
from src.domain.permutation import full_distribution, remove_kernel, tail_count
from src.utils.exports import write_report

PARAMS = {"alpha": 0.05, "min_pairs": 30, "min_gain_pp": 5}


def brute_force_p(d_values, statistic):
    """2^K 种整批交换的暴力枚举，作为精确卷积的对照。"""
    count = 0
    for signs in itertools.product((1, -1), repeat=len(d_values)):
        if abs(sum(sign * value for sign, value in zip(signs, d_values))) >= abs(statistic):
            count += 1
    return Fraction(count, 1 << len(d_values))


def rows_from_diffs(d_values, pairs_per_batch=8):
    """按各批次净差 d 构造配对数据：b_only−a_only=d，其余票两引擎均通过。"""
    rows = []
    for index, diff in enumerate(d_values):
        b_only, a_only = max(diff, 0), max(-diff, 0)
        both = pairs_per_batch - b_only - a_only
        assert both >= 0
        for (a_pass, b_pass), count in (
                ((True, True), both), ((True, False), a_only), ((False, True), b_only)):
            for _ in range(count):
                rows.append({"document_id": f"T-{len(rows) + 1:03d}", "batch_id": f"B{index + 1}",
                             "document_type": "增值税发票", "a_pass": a_pass, "b_pass": b_pass})
    return rows


class DictStore:
    """模拟内容指纹缓存：JSON 往返以贴近 JSONB 存取，并记录读取键。"""

    def __init__(self):
        self.data = {}
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        value = self.data.get(key)
        return json.loads(json.dumps(value)) if value is not None else None

    def set(self, key, payload):
        self.data.setdefault(key, json.loads(json.dumps(payload)))


def table_of(result, title):
    return next(table for table in result["tables"] if table["title"] == title)


class PermutationTest(unittest.TestCase):
    def test_distribution_matches_brute_force(self):
        cases = [[], [0], [3], [2, -1], [4, 0, 2], [3, 3, 1, -2, 5], [1] * 8, [7, -3, 2, 2, 0, 1]]
        for d_values in cases:
            offset, counts = full_distribution(d_values)
            self.assertEqual(sum(counts), 1 << len(d_values))
            statistic = sum(d_values)
            p_value = Fraction(tail_count(offset, counts, statistic), 1 << len(d_values))
            self.assertEqual(p_value, brute_force_p(d_values, statistic), d_values)

    def test_remove_kernel_matches_reduced_distribution(self):
        d_values = [4, 2, 0, -3, 1, 5]
        offset, counts = full_distribution(d_values)
        for index, removed in enumerate(d_values):
            sub_offset, sub_counts = remove_kernel(offset, counts, removed)
            rest = d_values[:index] + d_values[index + 1:]
            exp_offset, exp_counts = full_distribution(rest)
            self.assertEqual((sub_offset, sub_counts), (exp_offset, exp_counts), removed)
            statistic = sum(rest)
            p_value = Fraction(tail_count(sub_offset, sub_counts, statistic), 1 << len(rest))
            self.assertEqual(p_value, brute_force_p(rest, statistic))

    def test_chunked_distribution_equals_one_shot(self):
        d_values = [3, -1, 4, 0, 2, 5, -2, 1, 6]
        states = []
        offset, counts = full_distribution(d_values, chunk=3,
                                           checkpoint=lambda s: states.append(dict(s)))
        self.assertEqual((offset, counts), full_distribution(d_values))
        self.assertEqual([state["upto"] for state in states], [3, 6, 9])
        resumed = full_distribution(d_values, resume=states[1])
        self.assertEqual(resumed, (offset, counts))


class DemoFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze(demo_rows(), PARAMS)

    def test_full_analysis_passes(self):
        result = self.result
        self.assertEqual(result["state"], "ok")
        self.assertIn("建议切换", result["headline"])
        metrics = {item["label"]: item["value"] for item in result["metrics"]}
        self.assertEqual(metrics["有效配对数"], 64)
        self.assertEqual(metrics["双侧精确 p 值"], "0.015625")
        self.assertEqual(metrics["候选 B 净增（百分点）"], "+18.75")
        self.assertEqual(metrics["失守批次数"], 0)

    def test_leave_one_batch_out_all_pass(self):
        robustness = table_of(self.result, "逐批剔除稳健性")
        self.assertEqual(len(robustness["rows"]), 8)
        for row in robustness["rows"]:
            self.assertEqual(row["status"], "全部达标", row)
            self.assertEqual(row["pairs"], 56)
            # 剔除 d=0 批次后交换方案不再翻倍，p=2/128；其余情形 p=4/128。
            expected = "0.015625" if row["batch_id"] == "CS-FP-1122-PM" else "0.03125"
            self.assertEqual(row["p_value"], expected, row)
        breached = table_of(self.result, "失守批次及对应门槛")
        self.assertEqual(breached["rows"], [])

    def test_exact_p_value_against_brute_force(self):
        d_values = [2, 2, 1, 0, 2, 2, 2, 1]
        self.assertEqual(Fraction(4, 256), brute_force_p(d_values, 12))

    def test_disagreement_list_starts_with_a_only(self):
        review = table_of(self.result, "差异票据复核清单")
        self.assertEqual(len(review["rows"]), 18)
        first_three = [row["document_id"] for row in review["rows"][:3]]
        self.assertEqual(first_three, ["CS-20261121-001", "CS-20261121-002", "CS-20261121-003"])
        self.assertTrue(all(row["outcome"] == "仅 A 通过" for row in review["rows"][:3]))

    def test_result_is_json_serializable(self):
        json.dumps(self.result)

    def test_tightened_gain_gate_flips_decision(self):
        result = analyze(demo_rows(), {**PARAMS, "min_gain_pp": 20})
        self.assertEqual(result["state"], "warning")
        self.assertIn("候选 B 净增", result["headline"])
        gates = table_of(result, "验收门槛（全量）")
        status = {row["gate"]: row["status"] for row in gates["rows"]}
        self.assertEqual(status["双侧精确 p 值"], "达标")
        self.assertEqual(status["候选 B 净增"], "未达标")
        breached = table_of(result, "失守批次及对应门槛")
        self.assertEqual(len(breached["rows"]), 7)  # 仅剔除 d=0 批次时净增 21.43 仍达标
        self.assertTrue(all(row["gate"] == "候选 B 净增" for row in breached["rows"]))


class BreachedBatchTest(unittest.TestCase):
    def test_breached_batches_listed_with_gates(self):
        # 六个批次净差 +2、一个批次净差 -1：全量 p=0.03125 达标，
        # 剔除任一 +2 批次后 p=0.0625 失守，剔除 -1 批次仍达标。
        rows = rows_from_diffs([2, 2, 2, 2, 2, 2, -1])
        result = analyze(rows, PARAMS)
        self.assertEqual(result["state"], "warning")
        self.assertIn("剔除批次", result["headline"])
        for batch_id in ("B1", "B2", "B3", "B4", "B5", "B6"):
            self.assertIn(batch_id, result["headline"])
        gates = table_of(result, "验收门槛（全量）")
        self.assertTrue(all(row["status"] == "达标" for row in gates["rows"]))
        robustness = table_of(result, "逐批剔除稳健性")
        status = {row["batch_id"]: row["status"] for row in robustness["rows"]}
        self.assertEqual(status["B7"], "全部达标")
        for batch_id in ("B1", "B2", "B3", "B4", "B5", "B6"):
            self.assertEqual(status[batch_id], "未达：双侧精确 p 值")
        breached = table_of(result, "失守批次及对应门槛")
        self.assertEqual(len(breached["rows"]), 6)
        self.assertEqual({row["gate"] for row in breached["rows"]}, {"双侧精确 p 值"})
        self.assertEqual({row["limit"] for row in breached["rows"]}, {"≤ 0.05"})
        metrics = {item["label"]: item["value"] for item in result["metrics"]}
        self.assertEqual(metrics["失守批次数"], 6)

    def test_single_batch_has_no_robustness_table(self):
        rows = [row for row in demo_rows() if row["batch_id"] == "CS-FP-1121-AM"]
        result = analyze(rows, PARAMS)
        self.assertIn("仅 1 个扫描批次", result["note"])
        self.assertNotIn("逐批剔除稳健性", [table["title"] for table in result["tables"]])
        self.assertEqual(result["state"], "warning")  # 单批次 p 值恒为 1


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.saved = (analysis.STATS_CHUNK, analysis.DIST_CHUNK, analysis.LOBO_CHUNK)
        analysis.STATS_CHUNK = analysis.DIST_CHUNK = analysis.LOBO_CHUNK = 2

    def tearDown(self):
        analysis.STATS_CHUNK, analysis.DIST_CHUNK, analysis.LOBO_CHUNK = self.saved

    def test_interrupted_run_resumes_from_checkpoint(self):
        rows = demo_rows()
        reference = analyze(rows, PARAMS, store=DictStore())

        class Crash(Exception):
            pass

        # 分块大小调为 2 后共 12 个断点（统计 4、分布 4、逐批剔除 4），逐个验证可续算。
        for crash_at in range(1, 13):
            states = []

            def checkpoint(state):
                states.append(json.loads(json.dumps(state)))
                if len(states) >= crash_at:
                    raise Crash()

            with self.assertRaises(Crash, msg=f"第 {crash_at} 个断点"):
                analyze(rows, PARAMS, checkpoint=checkpoint, store=DictStore())
            result = analyze(rows, PARAMS, resume=states[-1], store=DictStore())
            self.assertEqual(json.dumps(reference, sort_keys=True),
                             json.dumps(result, sort_keys=True), f"第 {crash_at} 个断点后续算结果不一致")

    def test_resume_with_limited_crashes_eventually_completes(self):
        rows = demo_rows()
        reference = analyze(rows, PARAMS, store=DictStore())
        budget = [3]

        def checkpoint(state):
            checkpoint.state = json.loads(json.dumps(state))
            budget[0] -= 1
            if budget[0] <= 0:
                raise Interrupt()

        class Interrupt(Exception):
            pass

        state = None
        for _ in range(5):
            budget[0] = 3
            try:
                result = analyze(rows, PARAMS, resume=state, checkpoint=checkpoint, store=DictStore())
                break
            except Interrupt:
                state = checkpoint.state
        else:
            self.fail("多次中断后仍无法完成")
        self.assertEqual(json.dumps(reference, sort_keys=True), json.dumps(result, sort_keys=True))


class CacheTest(unittest.TestCase):
    def test_unchanged_batches_reused_after_source_update(self):
        store = DictStore()
        first = analyze(demo_rows(), PARAMS, store=store)
        self.assertEqual(first["cache"]["batch_misses"], 8)
        self.assertEqual(first["cache"]["batch_hits"], 0)

        # 源数据更新：一张两引擎均通过的票改为均未通过（净差不变），仅影响一个批次。
        updated = [dict(row) for row in demo_rows()]
        target = next(row for row in updated
                      if row["batch_id"] == "CS-XP-1122-PM" and row["a_pass"] and row["b_pass"])
        target["a_pass"] = target["b_pass"] = False
        second = analyze(updated, PARAMS, store=store)
        self.assertEqual(second["cache"]["batch_hits"], 7)
        self.assertEqual(second["cache"]["batch_misses"], 1)
        self.assertTrue(second["cache"]["dist_reused"])  # 净差多重集未变，置换分布沿用
        self.assertEqual(table_of(second, "验收门槛（全量）")["rows"],
                         table_of(first, "验收门槛（全量）")["rows"])

    def test_changed_batch_recomputed_and_distribution_rebuilt(self):
        store = DictStore()
        analyze(demo_rows(), PARAMS, store=store)
        updated = [dict(row) for row in demo_rows()]
        target = next(row for row in updated
                      if row["batch_id"] == "CS-FP-1122-PM" and row["a_pass"] and row["b_pass"])
        target["a_pass"] = False  # 均通过 → 仅 B 通过，净差变化
        result = analyze(updated, PARAMS, store=store)
        self.assertEqual(result["cache"]["batch_hits"], 7)
        self.assertEqual(result["cache"]["batch_misses"], 1)
        self.assertFalse(result["cache"]["dist_reused"])
        metrics = {item["label"]: item["value"] for item in result["metrics"]}
        self.assertEqual(metrics["双侧精确 p 值"], "0.0078125")  # 净差合计 13，p=2/256

    def test_stale_cache_entries_never_join_new_report(self):
        store = DictStore()
        analyze(demo_rows(), PARAMS, store=store)
        changed_batch = "CS-FP-1122-PM"
        old_members = sorted((row for row in demo_rows() if row["batch_id"] == changed_batch),
                             key=lambda row: row["document_id"])
        stale_key = "batch:" + batch_fingerprint(old_members)
        self.assertIn(stale_key, store.data)

        store.gets.clear()
        updated = [dict(row) for row in demo_rows()]
        target = next(row for row in updated
                      if row["batch_id"] == changed_batch and row["a_pass"] and not row["b_pass"])
        target["b_pass"] = True  # 仅 A 通过 → 均通过，旧批次指纹作废
        result = analyze(updated, PARAMS, store=store)
        # 旧键仍在缓存中，但新报告绝不读取：旧计算晚到也不带入新报告。
        self.assertIn(stale_key, store.data)
        self.assertNotIn(stale_key, store.gets)
        review = table_of(result, "差异票据复核清单")
        self.assertEqual(len([row for row in review["rows"] if row["outcome"] == "仅 A 通过"]), 2)


class ExportTest(unittest.TestCase):
    def test_report_lists_breached_batches_and_limits(self):
        rows = rows_from_diffs([2, 2, 2, 2, 2, 2, -1])
        result = analyze(rows, PARAMS)
        name = write_report(901, 1, result)
        with open(os.path.join(os.environ["ARTIFACT_DIR"], name), encoding="utf-8-sig") as handle:
            content = handle.read()
        self.assertIn("失守批次及对应门槛", content)
        self.assertIn("逐批剔除稳健性", content)
        for batch_id in ("B1", "B2", "B3", "B4", "B5", "B6"):
            self.assertIn(batch_id, content)
        self.assertIn("双侧精确 p 值", content)


class ValidationTest(unittest.TestCase):
    def test_duplicate_document_rejected(self):
        rows = demo_rows()
        rows.append(dict(rows[0]))
        with self.assertRaises(ValueError):
            analyze(rows, PARAMS)

    def test_unknown_param_rejected(self):
        with self.assertRaises(ValueError):
            analyze(demo_rows(), {**PARAMS, "unknown": 1})

    def test_min_pairs_gate(self):
        result = analyze(demo_rows(), {**PARAMS, "min_pairs": 100})
        gates = table_of(result, "验收门槛（全量）")
        status = {row["gate"]: row["status"] for row in gates["rows"]}
        self.assertEqual(status["有效配对数"], "未达标")
        self.assertEqual(result["state"], "warning")


if __name__ == "__main__":
    unittest.main()
