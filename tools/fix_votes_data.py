# -*- coding: utf-8 -*-
"""一次性修数据：把 OCR 读错的票数和空值按前后插值补回来，再重算派生列。

判定坏值：票数只增不减，所以
  - 空值 → 坏
  - 比上一个有效值跌超过 10% → 坏
  - 5 分钟内涨幅超过 50% → 坏
"""
import csv, os, shutil
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(BASE, "votes.csv")
BAK = os.path.join(BASE, "votes_raw_backup.csv")
COLS = ["总票数", "白小纯", "青稚", "Paraboy", "33Svan"]
ORDER = ["白小纯", "青稚", "Paraboy", "33Svan"]

shutil.copy(CSV, BAK)
with open(CSV, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
head, body = rows[0], rows[1:]


def num(x):
    try:
        return int(str(x).replace(",", ""))
    except Exception:
        return None


def tsec(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


data = [{c: num(r[i + 1]) for i, c in enumerate(COLS)} for r in body]
ts = [tsec(r[0]) for r in body]
n = len(data)

bad = [set() for _ in range(n)]
fixes = []

for ci, col in enumerate(COLS):
    prev_v, prev_i = None, None
    for i in range(n):
        v = data[i][col]
        if prev_v is None:
            if v is None:
                bad[i].add(col)
            else:
                prev_v, prev_i = v, i
            continue
        if v is None:
            bad[i].add(col)
            continue
        gap = (ts[i] - ts[prev_i]) / 60
        # 正常单轮增量不到 2 万。用绝对增量卡比看倍数可靠：8980809 相对 6074769 只有 1.48 倍，
        # 倍数阈值会放过它，但它比上一轮多了 290 万，绝无可能。
        limit = 300000 * max(gap / 5.0, 1.0)
        if v < prev_v * 0.9 or (v - prev_v) > limit:
            bad[i].add(col)
        else:
            prev_v, prev_i = v, i

for ci, col in enumerate(COLS):
    # 该列的中位增速（票/分钟），用于单边缺失时外推
    rates = []
    last = None
    for i in range(n):
        if col in bad[i] or data[i][col] is None:
            continue
        if last is not None:
            dv = data[i][col] - data[last][col]
            dm = (ts[i] - ts[last]) / 60
            if dm > 0 and dv >= 0:
                rates.append(dv / dm)
        last = i
    rates.sort()
    rate = rates[len(rates) // 2] if rates else 5000.0

    for i in range(n):
        if col not in bad[i]:
            continue
        old = data[i][col]
        a = next((j for j in range(i - 1, -1, -1) if col not in bad[j]), None)
        b = next((j for j in range(i + 1, n) if col not in bad[j]), None)
        if a is not None and b is not None:
            span = ts[b] - ts[a]
            w = (ts[i] - ts[a]) / span if span else 0.5
            new = round(data[a][col] + (data[b][col] - data[a][col]) * w)
        elif a is not None:
            new = round(data[a][col] + rate * (ts[i] - ts[a]) / 60)
        elif b is not None:
            new = round(data[b][col] - rate * (ts[b] - ts[i]) / 60)
        else:
            continue
        data[i][col] = new
        fixes.append((body[i][0], col, old, new))

# 保证单调不减（插值后仍可能有微小倒挂）
for ci, col in enumerate(COLS):
    for i in range(1, n):
        if data[i][col] < data[i - 1][col]:
            data[i][col] = data[i - 1][col]

out = [head]
for i, r in enumerate(body):
    d = data[i]
    dtot = dsum = share = None
    if i > 0:
        p = data[i - 1]
        if d["总票数"] and p["总票数"]:
            dtot = d["总票数"] - p["总票数"]
        diffs = [d[c] - p[c] for c in ORDER if d[c] and p[c]]
        if diffs:
            dsum = sum(diffs)
        if dtot and dsum is not None and dtot > 0:
            share = round(dsum / dtot * 100, 2)
    out.append([r[0]] + [d[c] for c in COLS] + [dtot, dsum, share])

with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
    csv.writer(f).writerows(out)

print("修复 %d 处，备份在 %s" % (len(fixes), BAK))
for t, c, o, nv in fixes:
    print("  %s  %-8s %s -> %s" % (t, c, o, nv))
