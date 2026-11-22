"""生成八个扫描批次的确定性匿名验收样本。

同批票据共用扫描条件，显著性按整批交换两引擎计算，2^K 种交换方案下
双侧精确 p 值最小为 2/2^K，因此演示数据覆盖 8 个批次（K=8），
让显著性门槛在 α=0.05 时可达。四格合计：均通过 38、仅 A 通过 3、
仅 B 通过 15、均未通过 8，共 64 对，候选 B 净增 18.75 个百分点。
"""


def demo_rows():
    # (批次, 类型, (均通过, 仅 A 通过, 仅 B 通过, 均未通过))，每批 8 张。
    batches = [
        ("CS-FP-1121-AM", "增值税发票", (5, 0, 2, 1)),
        ("CS-FP-1121-PM", "增值税发票", (5, 0, 2, 1)),
        ("CS-FP-1122-AM", "增值税发票", (4, 1, 2, 1)),
        ("CS-FP-1122-PM", "增值税发票", (3, 2, 2, 1)),
        ("CS-XP-1121-AM", "零售小票", (5, 0, 2, 1)),
        ("CS-XP-1121-PM", "零售小票", (5, 0, 2, 1)),
        ("CS-XP-1122-AM", "零售小票", (5, 0, 2, 1)),
        ("CS-XP-1122-PM", "零售小票", (6, 0, 1, 1)),
    ]
    outcomes = ((True, True), (True, False), (False, True), (False, False))
    a_only, rest = [], []
    for batch_id, document_type, counts in batches:
        for (a_pass, b_pass), count in zip(outcomes, counts):
            target = a_only if (a_pass, b_pass) == (True, False) else rest
            for _ in range(count):
                target.append({"batch_id": batch_id, "document_type": document_type,
                               "a_pass": a_pass, "b_pass": b_pass})
    # 仅 A 通过的票据编号排在最前，差异复核从 CS-20261121-001 起优先追查。
    return [{"document_id": f"CS-20261121-{index:03d}", **row}
            for index, row in enumerate(a_only + rest, 1)]
