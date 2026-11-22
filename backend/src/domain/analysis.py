"""校验配对票据，按整批交换精确置换检验运行验收。

同批票据共用扫描条件，显著性不再逐票翻转，而是整批交换两引擎：
批次净差 d = 仅 B 通过 − 仅 A 通过，原假设下整批交换 A/B 判定后 d 等概率变号。
样本数与净增门槛照旧，在全量数据和每一种逐批剔除情形上分别判定，
全部达标才建议切换；失守批次及对应门槛写入结果与导出。

大批量分块计算并落盘断点，中断后续算；批次统计与置换分布按内容指纹缓存，
源数据更新后沿用未受影响的结果，受影响批次重算，旧计算晚到也不并入新报告。
"""

import hashlib
import json
from collections import Counter
from decimal import Decimal, localcontext
from fractions import Fraction

from .permutation import full_distribution, remove_kernel, tail_count
from .spec import SPEC

STATS_CHUNK = 128
DIST_CHUNK = 256
LOBO_CHUNK = 64


class _NullStore:
    """默认空缓存：不读不写，analyze 仍可独立运行。"""

    def get(self, _key):
        return None

    def set(self, _key, _payload):
        pass


def validate_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("票据配对明细至少需要一条记录。")

    documents = []
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条票据配对格式有误。")
        document = {}
        for field in SPEC["fields"]:
            key, label, kind = field["key"], field["label"], field["type"]
            value = row.get(key)
            if kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"第 {index} 条的{label}须提供布尔判定。")
            else:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
                    raise ValueError(f"第 {index} 条的{label}格式有误。")
                value = value.strip()
                if kind == "select" and value not in field["options"]:
                    raise ValueError(f"第 {index} 条的{label}超出选项范围。")
            document[key] = value
        document_id = document["document_id"]
        if document_id in seen:
            raise ValueError(f"票据编号重复：{document_id}。")
        seen.add(document_id)
        documents.append(document)
    return documents


def _run_parameters(params):
    fields = SPEC["params"]
    if not isinstance(params, dict) or set(params) - {field["key"] for field in fields}:
        raise ValueError("验收参数格式有误或包含未知字段。")

    options = {}
    for field in fields:
        key = field["key"]
        value = params.get(key, field["default"])
        if (type(value) not in (int, float) or not field["min"] <= value <= field["max"]
                or (field["type"] == "int" and type(value) is not int)):
            raise ValueError(f'{field["label"]}须为 {field["min"]} 至 {field["max"]} 之间的有效数值。')
        options[key] = value
    return options


def batch_fingerprint(members):
    """单批次票据明细的内容指纹：同内容同指纹，改动即换指纹。"""
    raw = json.dumps(members, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def summarize(members):
    """单批次四格统计，是跨运行沿用的最小结果单元。"""
    counts = Counter((row["a_pass"], row["b_pass"]) for row in members)
    return {
        "pairs": sum(counts.values()),
        "both_pass": counts[True, True],
        "a_only": counts[True, False],
        "b_only": counts[False, True],
        "both_fail": counts[False, False],
    }


def _display_p_value(value):
    with localcontext() as context:
        context.prec = 12
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _gain_text(gain_pp):
    if gain_pp is None:
        return "—"
    return f"{float(gain_pp):+.4f}".rstrip("0").rstrip(".")


def _gates(options, pairs, gain_pp, p_value):
    """样本数与净增门槛照旧，显著性门槛用整批交换精确 p 值。"""
    alpha = str(options["alpha"])
    min_gain = str(options["min_gain_pp"])
    return [
        {"gate": "有效配对数", "observed": pairs, "limit": f"≥ {options['min_pairs']} 对",
         "passed": pairs >= options["min_pairs"]},
        {"gate": "双侧精确 p 值", "observed": _display_p_value(p_value), "limit": f"≤ {alpha}",
         "passed": p_value <= Fraction(alpha)},
        {"gate": "候选 B 净增", "observed": f"{_gain_text(gain_pp)} 个百分点" if gain_pp is not None else "无有效配对",
         "limit": f"≥ {min_gain} 个百分点且为正",
         "passed": gain_pp is not None and 0 < gain_pp >= Fraction(min_gain)},
    ]


def _failed(gates):
    return [gate["gate"] for gate in gates if not gate["passed"]]


def _table(title, columns, rows):
    return {
        "title": title,
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": rows,
    }


def _leave_one_out(batch_id, stats_map, offset, counts, batch_ids):
    """剔除一个批次后的原始量：配对数、净差、置换尾数。门槛在汇总时判定。"""
    rest = [item for item in batch_ids if item != batch_id]
    sub_offset, sub_counts = remove_kernel(offset, counts,
                                           stats_map[batch_id]["b_only"] - stats_map[batch_id]["a_only"])
    pairs = sum(stats_map[item]["pairs"] for item in rest)
    diff = sum(stats_map[item]["b_only"] - stats_map[item]["a_only"] for item in rest)
    return {"pairs": pairs, "diff": diff, "tail": tail_count(sub_offset, sub_counts, diff)}


def _scenario_view(batch_id, raw, options, rest_count):
    pairs, diff = raw["pairs"], raw["diff"]
    gain = Fraction(100 * diff, pairs) if pairs else None
    p_value = Fraction(raw["tail"], 1 << rest_count)
    gates = _gates(options, pairs, gain, p_value)
    return {"batch_id": batch_id, "pairs": pairs, "gain": gain, "p_value": p_value,
            "gates": gates, "failed": _failed(gates)}


def _collect_stats(grouped, batch_ids, state, store, checkpoint):
    """阶段一：逐批四格统计。按内容指纹缓存，源数据更新后沿用未受影响批次。"""
    stats_map = state.get("stats", {})
    hits = state.get("cache_hits", 0)
    misses = state.get("cache_misses", 0)
    pending = [batch_id for batch_id in batch_ids if batch_id not in stats_map]
    for start in range(0, len(pending), STATS_CHUNK):
        for batch_id in pending[start:start + STATS_CHUNK]:
            members = sorted(grouped[batch_id], key=lambda row: row["document_id"])
            key = "batch:" + batch_fingerprint(members)
            cached = store.get(key)
            if cached is None:
                misses += 1
                cached = summarize(members)
                store.set(key, cached)
            else:
                hits += 1
            stats_map[batch_id] = cached
        if checkpoint:
            checkpoint({"phase": "stats", "stats": stats_map, "cache_hits": hits, "cache_misses": misses})
    return stats_map, hits, misses


def _collect_distribution(d_values, state, store, stats_map, hits, misses, checkpoint):
    """阶段二：全量置换分布。按 |d| 多重集指纹缓存，未受影响时整体沿用。"""
    dist_key = "dist:" + hashlib.sha256(
        json.dumps(sorted(abs(value) for value in d_values), separators=(",", ":")).encode()
    ).hexdigest()
    cached = store.get(dist_key)
    if cached is not None:
        return int(cached["offset"]), [int(value) for value in cached["counts"]], True

    def progress(partial):
        if checkpoint:
            checkpoint({"phase": "dist", "stats": stats_map, "cache_hits": hits,
                        "cache_misses": misses, "dist": partial})

    offset, counts = full_distribution(d_values, resume=state.get("dist"),
                                       checkpoint=progress, chunk=DIST_CHUNK)
    store.set(dist_key, {"offset": offset, "counts": counts})
    return offset, counts, False


def _collect_scenarios(stats_map, offset, counts, batch_ids, state, hits, misses, checkpoint):
    """阶段三：逐批剔除。每批反卷积一次，分块落盘断点。"""
    scenarios = state.get("scenarios", {})
    if len(batch_ids) < 2:
        return scenarios
    pending = [batch_id for batch_id in batch_ids if batch_id not in scenarios]
    for start in range(0, len(pending), LOBO_CHUNK):
        for batch_id in pending[start:start + LOBO_CHUNK]:
            scenarios[batch_id] = _leave_one_out(batch_id, stats_map, offset, counts, batch_ids)
        if checkpoint:
            checkpoint({"phase": "lobo", "stats": stats_map, "cache_hits": hits,
                        "cache_misses": misses, "scenarios": scenarios})
    return scenarios


def analyze(rows, normalized_params, *, resume=None, checkpoint=None, store=None):
    """运行验收。resume 为上次断点，checkpoint 用于分块落盘，store 为内容指纹缓存。"""
    documents = validate_rows(rows)
    options = _run_parameters(normalized_params)
    store = store if store is not None else _NullStore()
    state = resume or {}
    phase = state.get("phase", "stats")

    grouped = {}
    for row in documents:
        grouped.setdefault(row["batch_id"], []).append(row)
    batch_ids = sorted(grouped)
    batch_count = len(batch_ids)

    stats_map = {key: value for key, value in state.get("stats", {}).items() if key in grouped}
    hits = state.get("cache_hits", 0)
    misses = state.get("cache_misses", 0)
    if phase == "stats":
        stats_map, hits, misses = _collect_stats(grouped, batch_ids, state, store, checkpoint)
        phase = "dist"

    d_values = [stats_map[item]["b_only"] - stats_map[item]["a_only"] for item in batch_ids]
    offset = counts = None
    dist_reused = False
    if phase == "dist":
        offset, counts, dist_reused = _collect_distribution(
            d_values, state, store, stats_map, hits, misses, checkpoint)
        phase = "lobo"
    if offset is None:  # 从 lobo 断点恢复时重取分布（缓存必命中或快速重算）
        offset, counts, dist_reused = _collect_distribution(
            d_values, {"dist": state.get("dist")}, store, stats_map, hits, misses, None)

    scenarios = {key: value for key, value in state.get("scenarios", {}).items() if key in grouped}
    if phase == "lobo":
        scenarios = _collect_scenarios(stats_map, offset, counts, batch_ids, state, hits, misses, checkpoint)

    totals = {"pairs": 0, "both_pass": 0, "a_only": 0, "b_only": 0, "both_fail": 0}
    for stats in stats_map.values():
        for key in totals:
            totals[key] += stats[key]
    pair_count = totals["pairs"]
    a_only, b_only = totals["a_only"], totals["b_only"]
    both_pass, both_fail = totals["both_pass"], totals["both_fail"]
    a_pass = both_pass + a_only
    b_pass = both_pass + b_only
    diff = b_only - a_only
    gain_pp = Fraction(100 * diff, pair_count)
    p_value = Fraction(tail_count(offset, counts, diff), 1 << batch_count)
    gates = _gates(options, pair_count, gain_pp, p_value)
    failed_gates = _failed(gates)

    views = [_scenario_view(batch_id, scenarios[batch_id], options, batch_count - 1)
             for batch_id in batch_ids if batch_id in scenarios]
    breached = [view for view in views if view["failed"]]

    if failed_gates:
        headline = f"暂缓切换。{'、'.join(failed_gates)}未达验收门槛。"
    elif breached:
        headline = f"暂缓切换。剔除批次{'、'.join(view['batch_id'] for view in breached)}后验收门槛失守。"
    else:
        headline = "全量与逐批剔除均达验收门槛，建议切换并提交差异复核。"

    note = (f"同批票据有效配对 {pair_count} 对，覆盖 {batch_count} 个扫描批次。"
            f"现用 A 通过 {a_pass} 对，候选 B 通过 {b_pass} 对。")
    if a_only == 0 and b_only == 0:
        note += "两引擎逐单判定一致，差异对为 0，p 值记为 1。"
    if batch_count < 2:
        note += "仅 1 个扫描批次，无法核对逐批剔除稳健性。"
    elif breached:
        note += f"失守批次 {len(breached)} 个：{'、'.join(view['batch_id'] for view in breached)}。"
    else:
        note += f"逐批剔除 {batch_count} 种情形均达标。"
    note += f"批次统计沿用缓存 {hits} 个、重算 {misses} 个"
    note += "，置换分布沿用缓存。" if dist_reused else "。"

    disagreements = []
    for document in sorted(documents, key=lambda row: (row["b_pass"], row["document_id"])):
        if document["a_pass"] == document["b_pass"]:
            continue
        improved = document["b_pass"]
        disagreements.append({
            "document_id": document["document_id"],
            "batch_id": document["batch_id"],
            "document_type": document["document_type"],
            "outcome": "仅 B 通过" if improved else "仅 A 通过",
            "next_step": "抽核 B 的通过判定" if improved else "优先核对 B 的未通过字段",
        })

    tables = [
        _table("配对四格表", (("a", "现用 A"), ("b_pass", "候选 B 通过"), ("b_fail", "候选 B 未通过")), [
            {"a": "通过", "b_pass": both_pass, "b_fail": a_only},
            {"a": "未通过", "b_pass": b_only, "b_fail": both_fail},
        ]),
        _table("验收门槛（全量）", (("gate", "验收项"), ("observed", "观测值"), ("limit", "门槛"), ("status", "判断")), [
            {"gate": gate["gate"], "observed": gate["observed"], "limit": gate["limit"],
             "status": "达标" if gate["passed"] else "未达标"}
            for gate in gates
        ]),
    ]
    if views:
        tables.append(_table("逐批剔除稳健性", (
            ("batch_id", "剔除批次"), ("pairs", "有效配对"), ("gain", "候选 B 净增"),
            ("p_value", "双侧精确 p 值"), ("status", "门槛判断"),
        ), [
            {"batch_id": view["batch_id"], "pairs": view["pairs"],
             "gain": f"{_gain_text(view['gain'])} 个百分点" if view["gain"] is not None else "无有效配对",
             "p_value": _display_p_value(view["p_value"]),
             "status": "全部达标" if not view["failed"] else f"未达：{'、'.join(view['failed'])}"}
            for view in views
        ]))
        tables.append(_table("失守批次及对应门槛", (
            ("batch_id", "失守批次"), ("gate", "验收项"), ("observed", "观测值"), ("limit", "门槛"),
        ), [
            {"batch_id": view["batch_id"], "gate": gate["gate"],
             "observed": gate["observed"], "limit": gate["limit"]}
            for view in breached for gate in view["gates"] if not gate["passed"]
        ]))
    tables.append(_table("差异票据复核清单", (
        ("document_id", "票据编号"), ("batch_id", "扫描批次"), ("document_type", "票据类型"),
        ("outcome", "配对判定"), ("next_step", "复核动作"),
    ), disagreements))

    robustness_text = "全量数据与每一种逐批剔除情形都达到样本数、显著性与净增门槛，才建议切换。"
    if views:
        robustness_text += (f"本次逐批剔除 {len(views)} 种情形，失守批次 {len(breached)} 个；"
                            "失守批次及对应门槛见下表，并随 CSV 一并导出。")
    else:
        robustness_text += "当前仅一个扫描批次，剔除检验不适用，结论依赖全量门槛。"

    return {
        "state": "warning" if (failed_gates or breached) else "ok",
        "headline": headline,
        "note": note,
        "metrics": [
            {"label": "有效配对数", "value": pair_count, "detail": f"差异对 {a_only + b_only} 对"},
            {"label": "现用 A 通过率", "value": f"{a_pass / pair_count:.2%}", "detail": f"{a_pass}/{pair_count} 对"},
            {"label": "候选 B 通过率", "value": f"{b_pass / pair_count:.2%}", "detail": f"{b_pass}/{pair_count} 对"},
            {"label": "候选 B 净增（百分点）", "value": _gain_text(gain_pp),
             "detail": f"仅 B 通过 {b_only} 对，减仅 A 通过 {a_only} 对"},
            {"label": "双侧精确 p 值", "value": _display_p_value(p_value),
             "detail": f"整批交换 {batch_count} 个批次，显著性水平 α = {options['alpha']}"},
            {"label": "失守批次数", "value": len(breached),
             "detail": "逐批剔除后门槛失守的批次" if batch_count >= 2 else "单批次无法核对"},
        ],
        "tables": tables,
        "charts": [{
            "title": "四类配对判定",
            "kind": "bar",
            "series": [
                {"label": label, "value": count}
                for label, count in (("均通过", both_pass), ("仅 A 通过", a_only),
                                     ("仅 B 通过", b_only), ("均未通过", both_fail))
            ],
        }, {
            "title": "各批次净差（仅 B 减仅 A）",
            "kind": "bar",
            "series": [{"label": batch_id, "value": stats_map[batch_id]["b_only"] - stats_map[batch_id]["a_only"]}
                       for batch_id in batch_ids],
        }],
        "sections": [
            {"title": "整批交换精确检验", "text": (
                f"同批票据共用扫描条件，显著性按整批交换两引擎计算：{batch_count} 个批次各可整体交换 "
                f"A/B 判定，共 2^{batch_count} 种交换方案。双侧精确 p 值为 |净差| 不小于观测值 "
                f"{diff} 的方案比例，以整数卷积精确展开，最少可达 { _display_p_value(Fraction(2, 1 << batch_count)) }。"
                "门槛判断使用舍入前的精确数值。未达到显著性门槛表示当前证据不足以确认差异。"
            )},
            {"title": "逐批剔除稳健性", "text": robustness_text},
            {"title": "分批计算与数据更新", "text": (
                "大批量分块计算并落盘断点，中断后从断点续算。批次统计与置换分布按内容指纹缓存："
                f"本次沿用批次统计 {hits} 个、重算 {misses} 个"
                f"，置换分布{'沿用缓存' if dist_reused else '重新展开'}。"
                "源数据更新后，未受影响批次的结果继续沿用，受影响批次的结果重算；"
                "旧版本晚到的计算不并入本报告。"
            )},
            {"title": "复核与交付", "text": (
                "先核对仅 A 通过的票据，再抽核仅 B 通过的票据。按票据编号回看影像与人工底单，"
                "补录复核决定后导出配对四格表、门槛判断、逐批剔除稳健性和差异清单。"
            )},
        ],
        "cache": {"batch_hits": hits, "batch_misses": misses, "dist_reused": dist_reused},
    }
