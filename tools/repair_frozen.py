"""回补被「沿用上一轮值」冻住的读数

旧版 sanitize 在读数不可信时会把上一轮的值抄进来，结果是这一轮 Δ=0、
下一轮把差额一次补回。本脚本按行时间找到当时的留档截图，单独重读这张卡片，
读到的值必须落在「上一行 < 本行 < 下一行」区间内才采用，否则按前后线性插值。

用法（项目根目录）：
    python tools/repair_frozen.py           # 只报告，不改文件
    python tools/repair_frozen.py --write   # 备份后写回
"""
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vote_scraper as vs  # noqa: E402

CSV_PATH = ROOT / "votes.csv"
BAK_PATH = ROOT / "votes_before_frozen_fix.csv"
SHOT_DIR = ROOT / "shots"
HEADER = ["时间", "总票数", "白小纯", "青稚", "Paraboy", "33Svan",
          "Δ总", "四人Δ合计", "四人Δ占比%"]
NAMES = {"r1l": "白小纯", "r1r": "青稚", "r2l": "Paraboy", "r2r": "33Svan"}
BLOCK = {"r1l": "top", "r1r": "top", "r2l": "bottom", "r2r": "bottom"}


def load():
    with CSV_PATH.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return int(str(x).replace(",", "").strip())
    except Exception:
        return None


def shot_for(row_time, key):
    stamp = row_time.replace("-", "").replace(" ", "_").replace(":", "")
    p = SHOT_DIR / ("%s_%s.jpg" % (BLOCK[key], stamp))
    return p if p.exists() else None


def main():
    write = "--write" in sys.argv
    rows = load()
    keys = ["total"] + vs.ORDER
    vals = [{k: num(r[c]) for k, c in zip(keys, HEADER[1:6])} for r in rows]

    # 冻结：本行某列与上一行完全相等。四位数选手每分钟都在涨，一分钟一点都不涨不可能是真的
    frozen = []
    for i in range(1, len(rows)):
        for k in vs.ORDER:
            a, b = vals[i - 1][k], vals[i][k]
            if a is not None and a == b:
                frozen.append((i, k, b))

    if not frozen:
        print("没有冻结行，无需修复")
        return

    scraper = vs.Scraper.__new__(vs.Scraper)
    scraper.ocr = vs.Ocr()
    scraper.gap_min = 1.0

    print("发现 %d 处冻结读数：" % len(frozen))
    changed = []
    # 倒序处理：下一行如果也冻着，先修好它，本行的合理区间才有意义
    for i, k, stuck in reversed(frozen):
        prev_v = vals[i - 1][k]
        next_v = vals[i + 1][k] if i + 1 < len(rows) else None
        how, got = "未修复", None

        p = shot_for(rows[i]["时间"], k)
        if p:
            from PIL import Image
            img = Image.open(p).convert("RGB")
            items = scraper.ocr.read(img)
            got = scraper.refine_vote([(img, items)], k, prev_v)
            if got is None:
                got = vs.extract_votes(items, img.width, img.height,
                                       prev={k: prev_v}, minutes=1.0).get(k)
            if got is None or not (got > prev_v and (next_v is None or got < next_v)):
                got = None
            else:
                how = "重读截图"
        if got is None:
            if next_v is not None:
                got = prev_v + round((next_v - prev_v) / 2)
                how = "前后插值"
            else:
                # 最后一行没有下一行可参照：按当时整体涨幅给个保守值
                got = prev_v + 6000
                how = "末尾保守估计"
        vals[i][k] = got
        changed.append((i, k, stuck, got, how, p.name if p else "无截图"))

    for i, k, stuck, got, how, shot in changed:
        print("  行%-3d %s  %-8s %d -> %d  (+%d)  %s  %s" % (
            i + 1, rows[i]["时间"], NAMES[k], stuck, got, got - stuck, how, shot))

    # 重算派生列
    for i, r in enumerate(rows):
        for k, c in zip(keys, HEADER[1:6]):
            r[c] = "" if vals[i][k] is None else str(vals[i][k])
        if i == 0:
            r["Δ总"], r["四人Δ合计"], r["四人Δ占比%"] = "", "", ""
            continue
        p = vals[i - 1]
        d_total = (vals[i]["total"] - p["total"]
                   if vals[i]["total"] and p["total"] else None)
        diffs = [vals[i][k] - p[k] for k in vs.ORDER
                 if vals[i][k] is not None and p[k] is not None]
        d_sum = sum(diffs) if diffs else None
        share = (round(d_sum / d_total * 100, 2)
                 if d_total and d_sum is not None and d_total > 0 else None)
        r["Δ总"] = "" if d_total is None else str(d_total)
        r["四人Δ合计"] = "" if d_sum is None else str(d_sum)
        r["四人Δ占比%"] = "" if share is None else str(share)

    if not write:
        print("\n（试运行，没有写文件。加 --write 才会备份并写回）")
        return

    shutil.copy2(CSV_PATH, BAK_PATH)
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    print("\n已写回 %s，备份 %s" % (CSV_PATH.name, BAK_PATH.name))


if __name__ == "__main__":
    main()
