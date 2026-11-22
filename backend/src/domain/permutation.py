"""整批交换两引擎的精确置换分布。

同批票据共用扫描条件，因此显著性不按逐票翻转，而是整批交换两引擎：
批次 i 的净差 d_i = 仅 B 通过 − 仅 A 通过，原假设下整批交换 A/B 判定后
d_i 等概率取正或负。T* = Σ ±d_i 的分布用纯整数卷积精确展开，
双侧精确 p 值为 |T*| ≥ |T_obs| 的交换方案数占 2^K 的比例（K 为批次数）。

分布以 (offset, counts) 表示：x 处的方案数为 counts[x - offset]。
卷积与反卷积全部为整数运算，任何规模下都保持精确。
"""


def convolve(offset, counts, d):
    """分布与两点核 {-d, +d} 卷积。d 取绝对值，d=0 时方案数整体翻倍。"""
    d = abs(d)
    if d == 0:
        return offset, [2 * value for value in counts]
    spread = [0] * (len(counts) + 2 * d)
    for index, value in enumerate(counts):
        if value:
            spread[index] += value
            spread[index + 2 * d] += value
    return offset - d, spread


def full_distribution(d_values, resume=None, checkpoint=None, chunk=256):
    """逐批卷积展开全量置换分布，可按 chunk 落盘断点、续算不丢已算部分。"""
    state = resume or {}
    offset = state.get("offset", 0)
    counts = [int(value) for value in state.get("counts", [1])]
    upto = state.get("upto", 0)
    for index in range(upto, len(d_values)):
        offset, counts = convolve(offset, counts, d_values[index])
        done = index + 1
        if checkpoint and (done % chunk == 0 or done == len(d_values)):
            checkpoint({"offset": offset, "counts": counts, "upto": done})
    return offset, counts


def remove_kernel(offset, counts, d):
    """从全量分布中剔除一个批次（反卷积），用于逐批剔除稳健性。

    g = f ⊗ {-d, +d} 时由 g 精确还原 f：自高端向下 f[x] = g[x+d] − f[x+2d]。
    d=0 的批次只让方案数翻倍，直接对折。
    """
    d = abs(d)
    if d == 0:
        return offset, [value // 2 for value in counts]
    size = len(counts) - 2 * d
    if size < 1:
        raise ValueError("置换分布与批次净差不一致，无法剔除该批次。")
    reduced = [0] * size
    for index in range(size - 1, -1, -1):
        higher = reduced[index + 2 * d] if index + 2 * d < size else 0
        reduced[index] = counts[index + 2 * d] - higher
    return offset + d, reduced


def tail_count(offset, counts, statistic):
    """|T*| ≥ |statistic| 的交换方案数（含观测情形自身）。"""
    bound = abs(statistic)
    return sum(value for index, value in enumerate(counts) if abs(offset + index) >= bound)
