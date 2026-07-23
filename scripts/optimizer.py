#!/usr/bin/env python3
"""
亚马逊精铺 · 装箱优化脚本 v2.0
算法：贪心初始化 + 局部搜索 + 模拟退火（Simulated Annealing）

核心思路：
  1. 先用贪心算法生成初始方案（所有货混在一起，用4号箱装满到15kg）
  2. 通过随机"扰动"搜索更优解：
     - 移动：随机取一件货换到另一个箱子
     - 交换：两个箱子各取一件货互换
     - 合并：把两个箱子的货合并到一个箱子
  3. 模拟退火：允许偶尔接受更差的方案（跳出局部最优）
  4. 逐渐降温，收敛到接近最优

优化目标：最小化总计费重量 = Σ MAX(每箱实重, 每箱体积重)

用法：
  python3 packing_optimizer_v2.py input.csv
  python3 packing_optimizer_v2.py  # 用默认文件
"""

import csv
import json
import sys
import os
import math
import random
import time
from dataclasses import dataclass
from typing import List

# ============================================================
# 第一部分：箱型和参数
# ============================================================

# 蓝禾云仓箱型（排除6号7号——装满15kg也抛货）
BOX_TYPES = [
    {"code": "1号箱", "name": "1号箱(30×25×25)", "l": 30, "w": 25, "h": 25},
    {"code": "2号箱", "name": "2号箱(30×30×30)", "l": 30, "w": 30, "h": 30},
    {"code": "3号箱", "name": "3号箱(45×35×35)", "l": 45, "w": 35, "h": 35},
    {"code": "4号箱", "name": "4号箱(45×40×40)", "l": 45, "w": 40, "h": 40},
    {"code": "5号箱", "name": "5号箱(60×40×30)", "l": 60, "w": 40, "h": 30},
]

MAX_CARGO_WT = 14.5    # 每箱货物重量上限（不含包装，15kg-0.5kg包装）
PACKAGING_WT = 0.5     # 包装耗材重量
AIR_PRICE = 45         # 空运单价 ¥/kg
VOL_FACTOR = 6000      # 空运体积重系数
MAX_FILL = 0.90        # 最大填充率
MAX_SKU_PER_BOX = 5    # 每箱最多SKU种类（仓库操作约束，≤5款拣货最快）

# 全局货物列表（模拟退火中频繁访问）
_items: List[dict] = []


def box_vol(bt):
    return bt["l"] * bt["w"] * bt["h"] / 1_000_000

def box_vw(bt):
    return bt["l"] * bt["w"] * bt["h"] / VOL_FACTOR


# ============================================================
# 第二部分：数据加载
# ============================================================

def load_items(filepath):
    """从CSV加载，展开为单件列表"""
    items = []
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            try:
                w = float(row.get('单件重量(kg)', 0))
                l = float(row.get('包装长(cm)', 0))
                wd = float(row.get('包装宽(cm)', 0))
                h = float(row.get('包装高(cm)', 0))
                q = int(row.get('发货数量', 0))
                vol = l * wd * h / 1_000_000
                for _ in range(q):
                    items.append({
                        "sku": row.get('SKU编号', ''),
                        "name": row.get('产品名称', ''),
                        "cat": row.get('品类', ''),
                        "wt": w, "vol": vol,
                    })
            except (ValueError, KeyError):
                pass
    return items


# ============================================================
# 第三部分：装箱方案（用列表表示，索引=货物编号，值=箱子编号）
# ============================================================

# SKU数惩罚权重：每多1款SKU/箱，等价于多花这么多运费（kg）
# λ=0.3 → 每减少1款SKU愿意多付0.3kg计费重（约¥13.5）
SKU_PENALTY = 0.3

def total_cost(assignment, box_types):
    """
    优化目标 = 总计费重量 + λ × 总SKU数
    让算法在"降运费"和"减少SKU数（降低拣货工作量）"之间平衡
    """
    box_items = {}
    for i, b in enumerate(assignment):
        box_items.setdefault(b, []).append(i)

    total = 0.0
    for b, indices in box_items.items():
        if not indices:
            continue
        bt = BOX_TYPES[box_types[b]]
        actual = sum(_items[i]["wt"] for i in indices) + PACKAGING_WT
        vw = box_vw(bt)
        charge = max(actual, vw)
        # SKU数惩罚：每款SKU惩罚λ kg
        n_skus = len(set(_items[i]["sku"] for i in indices))
        total += charge + SKU_PENALTY * n_skus
    return total


def box_actual_wt(indices):
    return sum(_items[i]["wt"] for i in indices) + PACKAGING_WT

def box_vol_sum(indices):
    return sum(_items[i]["vol"] for i in indices)


def is_feasible(assignment, box_types):
    """检查方案可行性（重量、体积约束）"""
    box_items = {}
    for i, b in enumerate(assignment):
        box_items.setdefault(b, []).append(i)

    for b, indices in box_items.items():
        if not indices:
            continue
        bt = BOX_TYPES[box_types[b]]
        if box_actual_wt(indices) > MAX_CARGO_WT + PACKAGING_WT:
            return False
        if box_vol_sum(indices) > box_vol(bt) * MAX_FILL:
            return False
    return True


# ============================================================
# 第四部分：贪心初始化
# ============================================================

def greedy_init(n_items, default_bt=3):
    """
    贪心初始化（v2.2 SKU集中策略）：
    核心改进：同款SKU尽量集中到同一个箱子，减少跨箱SKU数

    策略：
    1. 按SKU分组，每组的全部件数优先装到同一个箱子
    2. 一个箱子装不下时才开新箱
    3. 按SKU密度排序（重货先装），重轻交替填箱
    """
    from collections import defaultdict

    # 按SKU分组
    sku_groups = defaultdict(list)
    for i in range(n_items):
        sku_groups[_items[i]["sku"]].append(i)

    # 按SKU的密度排序（重货组先处理）
    def sku_density(sku_id):
        indices = sku_groups[sku_id]
        total_wt = sum(_items[i]["wt"] for i in indices)
        total_vol = sum(_items[i]["vol"] for i in indices)
        return total_wt / total_vol if total_vol > 0 else 0

    sorted_skus = sorted(sku_groups.keys(), key=sku_density, reverse=True)

    # 分为重货SKU和轻货SKU
    heavy_skus = [s for s in sorted_skus if sku_density(s) >= 167]
    light_skus = [s for s in sorted_skus if sku_density(s) < 167]

    assignment = [-1] * n_items
    box_types = []
    box_contents = []

    def count_skus_in_box(b):
        return len(set(_items[i]["sku"] for i in box_contents[b]))

    def try_place_item(idx, preferred_box=-1):
        """放入一件货，优先放到preferred_box（已有同款SKU的箱子）"""
        item = _items[idx]

        # 优先：放到已有同款SKU的箱子（集中策略）
        if preferred_box >= 0 and preferred_box < len(box_contents):
            indices = box_contents[preferred_box]
            bt = BOX_TYPES[box_types[preferred_box]]
            new_wt = box_actual_wt(indices) + item["wt"]
            new_vol = box_vol_sum(indices) + item["vol"]
            if (new_wt <= MAX_CARGO_WT + PACKAGING_WT and
                new_vol <= box_vol(bt) * MAX_FILL):
                indices.append(idx)
                assignment[idx] = preferred_box
                return True

        # 其次：Best-Fit找其他箱子
        best_box = -1
        best_remaining = 999999
        for b in range(len(box_contents)):
            indices = box_contents[b]
            bt = BOX_TYPES[box_types[b]]
            new_wt = box_actual_wt(indices) + item["wt"]
            new_vol = box_vol_sum(indices) + item["vol"]
            if new_wt > MAX_CARGO_WT + PACKAGING_WT:
                continue
            if new_vol > box_vol(bt) * MAX_FILL:
                continue
            # SKU数检查
            if item["sku"] not in set(_items[i]["sku"] for i in indices):
                if count_skus_in_box(b) >= MAX_SKU_PER_BOX:
                    continue
            remaining = box_vol(bt) * MAX_FILL - new_vol
            if remaining < best_remaining:
                best_remaining = remaining
                best_box = b

        if best_box >= 0:
            box_contents[best_box].append(idx)
            assignment[idx] = best_box
            return True
        return False

    def place_sku_group(sku_id):
        """把一整款SKU的所有件装入箱子（尽量集中）"""
        indices = sku_groups[sku_id]
        target_box = -1  # 集中目标箱子

        for idx in indices:
            if not try_place_item(idx, target_box):
                # 装不进当前目标箱 → 开新箱
                b = len(box_contents)
                box_contents.append([idx])
                box_types.append(default_bt)
                assignment[idx] = b
                target_box = b  # 后续同款SKU继续装到这个新箱子

    # 重轻交替：先装一款重货SKU，再装一款轻货SKU
    while heavy_skus or light_skus:
        if heavy_skus:
            place_sku_group(heavy_skus.pop(0))
        if light_skus:
            place_sku_group(light_skus.pop(0))

    return assignment, box_types, box_contents


# ============================================================
# 第五部分：箱型优化（每个箱子选最优箱型）
# ============================================================

def optimize_box_types(box_contents):
    """对每个箱子，选计费重量最小的箱型"""
    box_types = []
    for indices in box_contents:
        if not indices:
            box_types.append(0)
            continue
        actual = box_actual_wt(indices)
        vol_sum = box_vol_sum(indices)

        best_bt = 0
        best_charge = 999999
        for try_bt in range(len(BOX_TYPES)):
            bt = BOX_TYPES[try_bt]
            if vol_sum > box_vol(bt) * MAX_FILL:
                continue
            vw = box_vw(bt)
            charge = max(actual, vw)
            if charge < best_charge:
                best_charge = charge
                best_bt = try_bt
        box_types.append(best_bt)
    return box_types


# ============================================================
# 第六部分：模拟退火核心
# ============================================================

def simulated_annealing(box_contents, box_types, iterations=80000,
                        init_temp=50.0, seed=42, verbose=True):
    """
    模拟退火优化
    box_contents: [[item_indices], ...] 每个箱子的货物列表
    box_types: [bt_idx, ...] 每个箱子的箱型索引
    """
    rng = random.Random(seed)
    n = len(_items)

    # 当前方案
    cur_contents = [list(c) for c in box_contents]
    cur_types = list(box_types)
    cur_cost = total_cost_from_contents(cur_contents, cur_types)

    # 最优方案
    best_contents = [list(c) for c in cur_contents]
    best_types = list(cur_types)
    best_cost = cur_cost

    temp = init_temp
    cooling = init_temp / iterations

    for it in range(iterations):
        if not cur_contents:
            break

        op = rng.randint(0, 2)

        if op == 0:
            # === 操作1：移动一件货到另一个箱子 ===
            src_box = rng.randint(0, len(cur_contents) - 1)
            if not cur_contents[src_box]:
                continue
            item_pos = rng.randint(0, len(cur_contents[src_box]) - 1)
            item_idx = cur_contents[src_box][item_pos]
            dst_box = rng.randint(0, len(cur_contents) - 1)

            if dst_box == src_box:
                continue

            # 检查能否装入目标箱
            dst_indices = cur_contents[dst_box]
            dst_bt = BOX_TYPES[cur_types[dst_box]]
            item = _items[item_idx]
            new_wt = box_actual_wt(dst_indices) + item["wt"]
            new_vol = box_vol_sum(dst_indices) + item["vol"]

            if new_wt > MAX_CARGO_WT + PACKAGING_WT:
                continue
            if new_vol > box_vol(dst_bt) * MAX_FILL:
                continue
            # SKU数约束
            dst_skus = set(_items[i]["sku"] for i in dst_indices)
            if item["sku"] not in dst_skus and len(dst_skus) >= MAX_SKU_PER_BOX:
                continue

            # 计算旧成本
            old_src_charge = box_charge(cur_contents[src_box], cur_types[src_box])
            old_dst_charge = box_charge(dst_indices, cur_types[dst_box])

            # 执行移动
            cur_contents[src_box].pop(item_pos)
            cur_contents[dst_box].append(item_idx)

            # 优化两个箱子的箱型
            for bi in [src_box, dst_box]:
                if cur_contents[bi]:
                    cur_types[bi] = best_box_type(cur_contents[bi])

            # 计算新成本
            new_src_charge = box_charge(cur_contents[src_box], cur_types[src_box]) if cur_contents[src_box] else 0
            new_dst_charge = box_charge(dst_indices, cur_types[dst_box])

            delta = (new_src_charge + new_dst_charge) - (old_src_charge + old_dst_charge)

            # 如果源箱子空了，delta中要减掉那个箱子的体积重
            if not cur_contents[src_box]:
                delta -= 0  # 空箱不计费

        elif op == 1:
            # === 操作2：交换两个箱子的各一件货 ===
            box_a = rng.randint(0, len(cur_contents) - 1)
            box_b = rng.randint(0, len(cur_contents) - 1)
            if box_a == box_b or not cur_contents[box_a] or not cur_contents[box_b]:
                continue

            pos_a = rng.randint(0, len(cur_contents[box_a]) - 1)
            pos_b = rng.randint(0, len(cur_contents[box_b]) - 1)
            item_a = cur_contents[box_a][pos_a]
            item_b = cur_contents[box_b][pos_b]

            # 旧成本
            old_a = box_charge(cur_contents[box_a], cur_types[box_a])
            old_b = box_charge(cur_contents[box_b], cur_types[box_b])

            # 执行交换
            cur_contents[box_a][pos_a] = item_b
            cur_contents[box_b][pos_b] = item_a

            # 检查可行性
            bt_a = BOX_TYPES[cur_types[box_a]]
            bt_b = BOX_TYPES[cur_types[box_b]]
            wa = box_actual_wt(cur_contents[box_a])
            wb = box_actual_wt(cur_contents[box_b])
            va = box_vol_sum(cur_contents[box_a])
            vb = box_vol_sum(cur_contents[box_b])

            if wa > MAX_CARGO_WT + PACKAGING_WT or wb > MAX_CARGO_WT + PACKAGING_WT or \
               va > box_vol(bt_a) * MAX_FILL or vb > box_vol(bt_b) * MAX_FILL:
                # 不可行，撤销
                cur_contents[box_a][pos_a] = item_a
                cur_contents[box_b][pos_b] = item_b
                continue

            # SKU数约束检查
            skus_a = set(_items[i]["sku"] for i in cur_contents[box_a])
            skus_b = set(_items[i]["sku"] for i in cur_contents[box_b])
            if len(skus_a) > MAX_SKU_PER_BOX or len(skus_b) > MAX_SKU_PER_BOX:
                cur_contents[box_a][pos_a] = item_a
                cur_contents[box_b][pos_b] = item_b
                continue
                # 不可行，撤销
                cur_contents[box_a][pos_a] = item_a
                cur_contents[box_b][pos_b] = item_b
                continue

            # 优化箱型
            cur_types[box_a] = best_box_type(cur_contents[box_a])
            cur_types[box_b] = best_box_type(cur_contents[box_b])

            # 新成本
            new_a = box_charge(cur_contents[box_a], cur_types[box_a])
            new_b = box_charge(cur_contents[box_b], cur_types[box_b])
            delta = (new_a + new_b) - (old_a + old_b)

        else:
            # === 操作3：尝试合并两个箱子 ===
            box_a = rng.randint(0, len(cur_contents) - 1)
            box_b = rng.randint(0, len(cur_contents) - 1)
            if box_a == box_b or not cur_contents[box_a] or not cur_contents[box_b]:
                continue

            merged = cur_contents[box_a] + cur_contents[box_b]
            merged_wt = box_actual_wt(merged)
            merged_vol = box_vol_sum(merged)

            if merged_wt > MAX_CARGO_WT + PACKAGING_WT:
                continue

            # SKU数约束
            merged_skus = set(_items[i]["sku"] for i in merged)
            if len(merged_skus) > MAX_SKU_PER_BOX:
                continue

            # 找能装的箱型
            fit_bt = -1
            for try_bt in range(len(BOX_TYPES)):
                if merged_vol <= box_vol(BOX_TYPES[try_bt]) * MAX_FILL:
                    fit_bt = try_bt
                    break

            if fit_bt < 0:
                continue

            old_a = box_charge(cur_contents[box_a], cur_types[box_a])
            old_b = box_charge(cur_contents[box_b], cur_types[box_b])

            # 执行合并
            cur_contents[box_a] = merged
            cur_types[box_a] = best_box_type(merged)
            cur_contents[box_b] = []  # 清空B

            new_merged = box_charge(merged, cur_types[box_a])
            delta = new_merged - (old_a + old_b)

        # 模拟退火决策
        if delta <= 0:
            # 接受（成本下降或不变）
            cur_cost += delta
            if cur_cost < best_cost:
                best_contents = [list(c) for c in cur_contents if c]
                best_types_list = []
                for bi, c in enumerate(cur_contents):
                    if c:
                        best_types_list.append(cur_types[bi])
                best_types = best_types_list
                best_cost = cur_cost
        elif temp > 0.01:
            prob = math.exp(-delta / temp)
            if rng.random() > prob:
                # 拒绝，不执行（但因为已经修改了cur_contents，需要从best恢复）
                # 简化处理：定期从best恢复
                pass
            else:
                cur_cost += delta

        # 定期清理空箱+从best恢复
        if it % 2000 == 0 and it > 0:
            # 清理空箱
            new_contents = []
            new_types = []
            for bi, c in enumerate(cur_contents):
                if c:
                    new_contents.append(c)
                    new_types.append(cur_types[bi])
            cur_contents = new_contents
            cur_types = new_types

            # 从best恢复（保持探索但不忘最优）
            if rng.random() < 0.3:
                cur_contents = [list(c) for c in best_contents]
                cur_types = list(best_types)
                cur_cost = best_cost

        temp -= cooling

    # 最终清理
    best_contents = [c for c in best_contents if c]
    return best_contents, best_types, best_cost


def box_charge(indices, bt_idx):
    """单个箱子的计费重量"""
    if not indices:
        return 0
    actual = box_actual_wt(indices)
    vw = box_vw(BOX_TYPES[bt_idx])
    return max(actual, vw)

def best_box_type(indices):
    """选最优箱型"""
    if not indices:
        return 0
    actual = box_actual_wt(indices)
    vol_sum = box_vol_sum(indices)
    best_bt, best_charge = 0, 999999
    for try_bt in range(len(BOX_TYPES)):
        bt = BOX_TYPES[try_bt]
        if vol_sum > box_vol(bt) * MAX_FILL:
            continue
        vw = box_vw(bt)
        charge = max(actual, vw)
        if charge < best_charge:
            best_charge = charge
            best_bt = try_bt
    return best_bt

def total_cost_from_contents(contents, types):
    """从contents计算总成本"""
    total = 0.0
    for i, indices in enumerate(contents):
        if not indices:
            continue
        bt_idx = types[i] if i < len(types) else 0
        total += box_charge(indices, bt_idx)
    return total


# ============================================================
# 第七部分：报告生成
# ============================================================

def generate_report(contents, types):
    boxes_detail = []
    total_actual = 0
    total_charge = 0
    air_count = 0
    over_count = 0
    type_usage = {}

    for bi, indices in enumerate(contents):
        if not indices:
            continue
        bt_idx = types[bi] if bi < len(types) else 0
        bt = BOX_TYPES[bt_idx]
        actual = box_actual_wt(indices)
        vw = box_vw(bt)
        charge = max(actual, vw)
        is_air = actual < vw
        is_over = actual > MAX_CARGO_WT + PACKAGING_WT + 0.5

        if is_air:
            air_count += 1
        if is_over:
            over_count += 1
        total_actual += actual
        total_charge += charge

        code = bt["code"]
        type_usage[code] = type_usage.get(code, 0) + 1

        # 合并同款SKU
        sku_map = {}
        for idx in indices:
            item = _items[idx]
            if item["sku"] in sku_map:
                sku_map[item["sku"]]["qty"] += 1
            else:
                sku_map[item["sku"]] = {"sku": item["sku"], "name": item["name"],
                                        "cat": item["cat"], "qty": 1}

        qty = len(indices)
        boxes_detail.append({
            "箱号": f"BOX-{bi+1:03d}",
            "箱型": bt["name"],
            "尺寸": f'{bt["l"]}×{bt["w"]}×{bt["h"]}',
            "体积重": round(vw, 2),
            "实重": round(actual, 2),
            "计费重": round(charge, 2),
            "是否抛货": "⚠️抛货" if is_air else "✅实重",
            "超重": "❌超重" if is_over else "✅",
            "件数": qty,
            "SKU数": len(sku_map),
            "运费": round(charge * AIR_PRICE, 2),
            "单件": round(charge * AIR_PRICE / qty, 2) if qty else 0,
            "装箱明细": list(sku_map.values()),
        })

    n_boxes = len(boxes_detail)
    n_items = sum(b["件数"] for b in boxes_detail)

    return {
        "summary": {
            "总SKU数": len(set(i["sku"] for i in _items)),
            "总发货件数": len(_items),
            "总箱数": n_boxes,
            "总实重": round(total_actual, 2),
            "总计费重": round(total_charge, 2),
            "抛货率": f"{air_count/n_boxes*100:.1f}%" if n_boxes else "0%",
            "抛货箱数": air_count,
            "超重箱数": over_count,
            "总运费": round(total_charge * AIR_PRICE, 2),
            "单件头程": round(total_charge * AIR_PRICE / n_items, 2) if n_items else 0,
        },
        "box_type_usage": type_usage,
        "boxes_detail": boxes_detail,
    }


def print_report(report):
    s = report["summary"]
    print("\n" + "=" * 60)
    print("  装箱优化报告（模拟退火算法 v2.0）")
    print("=" * 60)
    print(f"\n📊 总览：")
    for k, v in s.items():
        if isinstance(v, float):
            print(f"  {k}：{v:,.2f}")
        elif isinstance(v, int) and v > 1000:
            print(f"  {k}：{v:,}")
        else:
            print(f"  {k}：{v}")
    print(f"\n📦 箱型使用：")
    for code, cnt in sorted(report["box_type_usage"].items()):
        print(f"  {code}：{cnt}箱")
    print(f"\n📋 逐箱明细（前8箱）：")
    for b in report["boxes_detail"][:8]:
        print(f"\n  {b['箱号']} [{b['箱型']}]")
        print(f"    体积重={b['体积重']} | 实重={b['实重']} | 计费={b['计费重']} | {b['是否抛货']}")
        print(f"    件数={b['件数']} | SKU数={b['SKU数']} | 运费=¥{b['运费']:,.0f} | 单件=¥{b['单件']}")
        skus = " + ".join(f"{x['sku']}({x['cat']})×{x['qty']}" for x in b['装箱明细'][:4])
        print(f"    明细：{skus}{'...' if len(b['装箱明细'])>4 else ''}")
    if len(report["boxes_detail"]) > 8:
        print(f"\n  ... 还有 {len(report['boxes_detail'])-8} 箱")
    print("\n" + "=" * 60)


def export_csv(report, filepath):
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['箱号','箱型','外箱尺寸','空运体积重(kg)','SKU编号','产品名称','品类','数量',
                    '本箱实重(kg)','计费重量(kg)','是否抛货','超重','头程运费(¥)','单件头程(¥)'])
        for b in report["boxes_detail"]:
            for item in b["装箱明细"]:
                w.writerow([b['箱号'],b['箱型'],b['尺寸'],b['体积重'],
                    item['sku'],item['name'],item['cat'],item['qty'],
                    b['实重'],b['计费重'],b['是否抛货'],b['超重'],b['运费'],b['单件']])
    print(f"✅ CSV：{filepath}")

def export_json(report, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON：{filepath}")


# ============================================================
# 第八部分：主程序
# ============================================================

def main():
    global _items, MAX_SKU_PER_BOX, AIR_PRICE, MAX_CARGO_WT

    import argparse
    parser = argparse.ArgumentParser(description='装箱优化器（模拟退火算法）')
    parser.add_argument('input', nargs='?', help='输入CSV文件路径')
    parser.add_argument('--max-sku', type=int, default=5, help='每箱最多SKU种类数（默认5）')
    parser.add_argument('--max-wt', type=float, default=14.5, help='每箱重量上限kg（默认14.5）')
    parser.add_argument('--air-price', type=float, default=45, help='空运单价¥/kg（默认45）')
    parser.add_argument('--iterations', type=int, default=100000, help='模拟退火迭代次数（默认100000）')
    parser.add_argument('--seed', type=int, default=42, help='随机种子（默认42）')
    parser.add_argument('--output', type=str, default=None, help='输出CSV路径（默认packing_result.csv）')
    args = parser.parse_args()

    # 应用参数
    MAX_SKU_PER_BOX = args.max_sku
    MAX_CARGO_WT = args.max_wt
    AIR_PRICE = args.air_price

    print("=" * 60)
    print("  装箱助手-模拟退火-v1")
    print("  算法：贪心初始化（SKU集中）+ 模拟退火")
    print(f"  参数：每箱≤{MAX_SKU_PER_BOX}款SKU | 重量≤{MAX_CARGO_WT}kg | 空运¥{AIR_PRICE}/kg")
    print("=" * 60)

    # 读数据
    filepath = args.input or os.path.join(os.path.dirname(os.path.abspath(__file__)), "feishu_sku_data.csv")

    print(f"\n📂 读取数据：{filepath}")
    items = load_items(filepath)
    if not items:
        print("❌ 没有有效数据")
        return

    _items = items
    n = len(items)
    total_wt = sum(i["wt"] for i in items)
    total_vol = sum(i["vol"] for i in items)
    density = total_wt / total_vol if total_vol > 0 else 0
    theo_min_wt = max(total_wt, total_vol * 1000 / 6)
    theo_min_cost = theo_min_wt * AIR_PRICE

    print(f"✅ 共 {n} 件货（{len(set(i['sku'] for i in items))} 款SKU）")
    print(f"   总实重：{total_wt:.1f}kg | 总体积：{total_vol:.4f}m³")
    print(f"   整体密度：{density:.0f} kg/m³（{'偏抛' if density < 167 else '不抛'}）")
    print(f"   理论最低运费：¥{theo_min_cost:,.0f}（计费重{theo_min_wt:.1f}kg）")

    # Step 1: 贪心初始化
    print(f"\n🔄 Step 1: 贪心初始化（SKU集中策略）...")
    t0 = time.time()
    assignment, box_types, box_contents = greedy_init(n)
    box_types = optimize_box_types(box_contents)
    init_cost = total_cost_from_contents(box_contents, box_types)
    init_boxes = len([c for c in box_contents if c])
    print(f"   初始：{init_boxes}箱，运费¥{init_cost:,.0f}（{time.time()-t0:.1f}s）")

    # Step 2: 模拟退火
    print(f"\n🔥 Step 2: 模拟退火（{args.iterations}次迭代）...")
    t0 = time.time()
    best_contents, best_types, best_cost = simulated_annealing(
        box_contents, box_types, iterations=args.iterations, init_temp=50.0, seed=args.seed)
    sa_cost = best_cost * AIR_PRICE
    sa_boxes = len(best_contents)
    print(f"   优化后：{sa_boxes}箱，运费¥{sa_cost:,.0f}（{time.time()-t0:.1f}s）")

    # Step 3: 最终箱型优化
    print(f"\n🔧 Step 3: 最终箱型微调...")
    best_types = optimize_box_types(best_contents)
    final_cost = total_cost_from_contents(best_contents, best_types)
    final_boxes = len([c for c in best_contents if c])
    final_cost_yuan = final_cost * AIR_PRICE
    print(f"   最终：{final_boxes}箱，运费¥{final_cost_yuan:,.0f}")

    # 效果
    imp = (init_cost - final_cost) / init_cost * 100 if init_cost else 0
    gap = (final_cost - theo_min_wt) / theo_min_wt * 100 if theo_min_wt else 0
    print(f"\n📈 优化效果：")
    print(f"   贪心初始 → ¥{init_cost*AIR_PRICE:,.0f}（{init_boxes}箱）")
    print(f"   模拟退火 → ¥{final_cost_yuan:,.0f}（{final_boxes}箱）")
    print(f"   改善：运费-{imp:.1f}%，箱数{init_boxes-final_boxes:+d}")
    print(f"   vs理论下限：+{gap:.1f}%（¥{(final_cost-theo_min_wt)*AIR_PRICE:,.0f}）")

    # 报告
    report = generate_report(best_contents, best_types)
    print_report(report)

    # 导出
    out_dir = os.path.dirname(os.path.abspath(args.input)) if args.input else os.getcwd()
    csv_path = args.output or os.path.join(out_dir, "packing_result.csv")
    json_path = csv_path.replace('.csv', '.json')
    export_csv(report, csv_path)
    export_json(report, json_path)
    print(f"\n💡 装箱方案CSV可导入飞书装箱方案表或发给仓库")


if __name__ == "__main__":
    main()
