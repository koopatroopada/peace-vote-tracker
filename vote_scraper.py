#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
vote_scraper.py —— 和平营地「巅峰挑战赛人气投票」票数定时采集

原理：截图 → OCR 读数字 → 坐标点击（退出/重进触发刷新）→ 追加 CSV

每轮动作：
  1. 若停在活动页，按 BACK 回到搜索页
  2. 点击搜索页横幅广告 → 进入活动页（WebView）
  3. 轮询截屏，连续两帧一致视为加载完成
  4. 下滑回顶部 → 截图 A（总票数 + 第 1 排：白小纯 / 青稚）
  5. 上滑 800px   → 截图 B（第 2 排：Paraboy / 33Svan）
  6. 两张图分别 OCR，按选手名锚点提取票数，合并
  7. 写 CSV，等 60 秒进入下一轮

为什么必须两段截图：页面底部有「投票记录 / 做任务赢 PEL」悬浮栏，
带渐变压暗，把第 2 排票数的下半截盖住，不滚动就永远读不准。

用法：
  python vote_scraper.py --dry-run     # 只截屏+OCR，不点击，用来校准
  python vote_scraper.py --once        # 完整跑一轮
  python vote_scraper.py               # 无限循环，默认 60 秒一轮
  python vote_scraper.py --interval 30

停止：Ctrl+C，或在脚本目录创建 .stop 文件
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
# 打包成 exe 后，资源会被解到临时目录，脚本无法再靠 __file__ 找同级文件
DATA_DIR = Path(os.environ.get("VOTE_TRACKER_HOME") or HERE)
SHOT_DIR = DATA_DIR / "shots"
CSV_PATH = DATA_DIR / "votes.csv"
STOP_FILE = DATA_DIR / ".stop"
CONFIG_PATH = DATA_DIR / "scraper_config.json"


def find_adb():
    """按优先级查找 adb：环境变量 → PATH → Android SDK 默认位置 → 程序同级目录"""
    exe_dir = Path(sys.executable).resolve().parent

    cands = []
    env = os.environ.get("ADB_PATH")
    if env:
        cands.append(env)
    w = shutil.which("adb")
    if w:
        cands.append(w)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
    for base in (DATA_DIR, exe_dir):
        cands.append(str(base / "platform-tools" / "adb.exe"))
        cands.append(str(base / "platform-tools" / "adb"))
    cands += [
        r"C:\Android\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        "/usr/local/bin/adb",
        "/usr/bin/adb",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return "adb"  # 兜底，交给系统 PATH，找不到时由 subprocess 抛错


ADB = find_adb()

# ── 基准截图 1224x2688（真机原生分辨率）上的锚点，其他分辨率自动缩放 ──
BASE_W, BASE_H = 1224, 2688
DEFAULT_CFG = {
    # 搜索页：横幅广告「2026 PMC 最高人气由你来定」中心点
    "tap_ad": [612, 660],
    # 上滑：把第 2 排票数推到屏幕中部，避开底部悬浮栏
    "swipe_up": [612, 2100, 612, 1300, 500],
    # 下滑：回到页面顶部
    "swipe_down": [612, 1100, 612, 2300, 250],
    # 加载等待
    "load_timeout": 15.0,
    "stable_gap": 0.5,
    # 页面加载后额外静置，等异步数据渲染完
    "settle": 1.2,
    # 截图留档：只在 need 模式下落盘，裁掉无关区域后转 JPEG
    "crop_top": [0, 850, 1224, 1850],      # 总票数 + 第 1 排（白小纯/青稚）
    "crop_bottom": [0, 900, 1224, 1700],   # 第 2 排（Paraboy/33Svan）
    "jpeg_quality": 82,
}

# 选手名 → CSV 字段名。名字必须能在 OCR 结果里找到（作为锚点）
PLAYERS = [
    ("白小纯", "r1l"),
    ("青稚", "r1r"),
    ("Paraboy", "r2l"),
    ("33Svan", "r2r"),
]
ORDER = ["r1l", "r1r", "r2l", "r2r"]
HEADERS = {"total": "总票数", "r1l": "白小纯", "r1r": "青稚",
           "r2l": "Paraboy", "r2r": "33Svan"}

MIN_VOTE = 10_000          # 票数量级下限，用于过滤噪音
MAX_SANE_JUMP = 5_000_000  # 单轮增量上限，超过视为识别错误


def load_config():
    cfg = dict(DEFAULT_CFG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print("配置读取失败，用默认值:", e)
    return cfg


# ──────────────────────────── ADB ────────────────────────────

class Adb:
    def __init__(self, serial=None):
        self.serial = serial

    def _args(self):
        return [ADB] + (["-s", self.serial] if self.serial else [])

    def run(self, *args, timeout=30, binary=False):
        r = subprocess.run(self._args() + list(args), capture_output=True, timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError("adb %s 失败: %s" % (" ".join(args), err))
        return r.stdout if binary else r.stdout.decode("utf-8", "ignore")

    def ensure_device(self):
        out = self.run("devices")
        lines = [l.split() for l in out.splitlines()[1:] if l.strip()]
        if not lines:
            raise RuntimeError("没有已连接的设备。")
        if self.serial is None:
            self.serial = lines[0][0]
        for parts in lines:
            if parts[0] == self.serial:
                if len(parts) > 1 and parts[1] != "device":
                    raise RuntimeError("设备状态是 %s，不接受调试命令。" % parts[1])
                return self.serial
        raise RuntimeError("找不到设备 %s" % self.serial)

    def size(self):
        out = self.run("shell", "wm", "size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise RuntimeError("读不到分辨率: %r" % out)
        return int(m[-1][0]), int(m[-1][1])

    def tap(self, x, y):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, dur):
        self.run("shell", "input", "swipe",
                 str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(dur)))

    def back(self):
        self.run("shell", "input", "keyevent", "KEYCODE_BACK")

    def wake(self):
        self.run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self.run("shell", "svc", "power", "stayon", "true")

    def top_activity(self):
        out = self.run("shell", "dumpsys", "activity", "activities", timeout=20)
        m = re.search(r"topResumedActivity=.*?\s(\S+)/(\S+)\s", out)
        return (m.group(1), m.group(2)) if m else (None, None)

    def screencap(self):
        data = self.run("exec-out", "screencap", "-p", timeout=40, binary=True)
        if len(data) < 2000:
            raise RuntimeError("截屏数据异常，只有 %d 字节" % len(data))
        return Image.open(io.BytesIO(data)).convert("RGB")


# ──────────────────────────── 画面指纹 ────────────────────────────

def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


# ──────────────────────────── OCR ────────────────────────────

class Ocr:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.engine = RapidOCR()
        self.calls = 0

    def read(self, img):
        """返回 [(text, cx, cy, score), ...]"""
        import numpy as np
        self.calls += 1
        out, _ = self.engine(np.array(img))
        items = []
        if not out:
            return items
        for box, text, score in out:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append((str(text).strip(), sum(xs) / 4, sum(ys) / 4, float(score)))
        return items


NUM_RE = re.compile(r"\d{1,3}(?:[,\s，]\d{3})+|\d{5,}")
TOTAL_RE = re.compile(r"总投票?数|总票数")


def to_int(s):
    return int(re.sub(r"\D", "", s))


def extract_total(items):
    """从「所有玩家总投票数：13758372」里取数字"""
    for text, cx, cy, score in items:
        if TOTAL_RE.search(text):
            tail = re.split(r"[:：]", text)[-1]
            m = NUM_RE.search(tail)
            if m and to_int(m.group()) >= MIN_VOTE:
                return to_int(m.group())
    return None


def extract_votes(items, w, h):
    """按选手名做锚点，取其下方 30~170px、同一半屏的票数

    不依赖固定 y 坐标，滚动量变动也能适应。
    """
    result = {"total": extract_total(items)}
    for name, key in PLAYERS:
        result.setdefault(key, None)
        ay = None
        for text, cx, cy, score in items:
            if name.lower() in text.lower():
                ay = cy
                break
        if ay is None:
            continue
        want_left = key.endswith("l")
        best = None
        for text, cx, cy, score in items:
            if not (ay + 25 <= cy <= ay + 175):
                continue
            if (cx < w / 2) != want_left:
                continue
            m = NUM_RE.search(text)
            if not m:
                continue
            v = to_int(m.group())
            if v < MIN_VOTE:
                continue
            if best is None or score > best[1]:
                best = (v, score)
        if best:
            result[key] = best[0]
    return result


def merge(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            if v is not None:
                out[k] = v
    return out


# ──────────────────────────── 主流程 ────────────────────────────

class Scraper:
    def __init__(self, cfg, interval=60.0, save_mode="need"):
        self.cfg = cfg
        self.interval = interval
        # save_mode: none 不存 / need 只在读数可疑时存 / all 每轮都存
        self.save_mode = save_mode
        self.last_images = None
        self.dev = Adb()
        self.w, self.h = self.dev.size()
        self.ocr = Ocr()
        print("设备: %s   分辨率: %dx%d   留档模式: %s" % (
            self.dev.ensure_device(), self.w, self.h, save_mode))

    def pt(self, pair):
        return pair[0] * self.w / BASE_W, pair[1] * self.h / BASE_H

    def swipe_cfg(self, key):
        x1, y1, x2, y2, dur = self.cfg[key]
        (x1, y1), (x2, y2) = self.pt((x1, y1)), self.pt((x2, y2))
        self.dev.swipe(x1, y1, x2, y2, dur)

    def wait_loaded(self):
        """连续两帧指纹一致 → 认为页面加载完成"""
        cfg = self.cfg
        deadline = time.monotonic() + cfg["load_timeout"]
        prev = None
        last = None
        while time.monotonic() < deadline:
            img = self.dev.screencap()
            cur = dhash(img)
            last = img
            if prev is not None and hamming(prev, cur) <= 2:
                time.sleep(cfg["settle"])
                return self.dev.screencap()
            prev = cur
            time.sleep(cfg["stable_gap"])
        print("  ! 加载超时，用最后一帧")
        return last

    def capture_pair(self):
        """截图 A（顶部）+ 上滑后截图 B（第 2 排）"""
        self.swipe_cfg("swipe_down")      # 无论停在哪儿，先回顶部
        time.sleep(0.5)
        a = self.dev.screencap()
        self.swipe_cfg("swipe_up")
        time.sleep(0.5)
        b = self.dev.screencap()
        return a, b

    def enter_activity(self):
        """保证从搜索页点广告进入活动页"""
        pkg, act = self.dev.top_activity()
        if act and "WebViewActivity" in act:
            self.dev.back()               # 已在活动页，先退回搜索页
            time.sleep(1.0)
        ax, ay = self.pt(self.cfg["tap_ad"])
        self.dev.tap(ax, ay)

    @staticmethod
    def suspect_fields(votes):
        """缺失，或比上一名次小 3 倍以上 → 判为识别可疑"""
        bad = []
        prev = None
        for k in ORDER:
            v = votes.get(k)
            if v is None:
                bad.append(k)
            elif prev and v < prev / 3:
                bad.append(k)
            if v is not None:
                prev = v
        return bad

    def save_shot(self, img, tag, full=False):
        """裁剪关键区域后存成 JPEG，单张约 100KB（全屏 PNG 是 2.4MB）"""
        SHOT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = img
        if not full:
            box = self.cfg.get("crop_top" if tag == "top" else "crop_bottom")
            if box:
                sx, sy = img.width / BASE_W, img.height / BASE_H
                x0, y0, x1, y1 = box
                out = img.crop((int(x0 * sx), int(y0 * sy),
                                int(x1 * sx), int(y1 * sy)))
        path = SHOT_DIR / ("%s_%s.jpg" % (tag, ts))
        out.save(path, "JPEG", quality=self.cfg["jpeg_quality"], optimize=True)
        return path

    def read_once(self, dry_run=False, save=False):
        if dry_run:
            a, b = self.capture_pair()
        else:
            self.enter_activity()
            self.wait_loaded()
            a, b = self.capture_pair()

        va = extract_votes(self.ocr.read(a), a.width, a.height)
        vb = extract_votes(self.ocr.read(b), b.width, b.height)
        votes = merge(va, vb)
        self.last_images = (a, b)   # 供 run() 在读数可疑时回捞留档

        # OCR 已经做完，压缩对结果零影响
        if save:
            self.save_shot(a, "top")
            self.save_shot(b, "bottom")
        return votes, {"top": va, "bottom": vb}

    def run(self, rounds=0, dry_run=False):
        n = 0
        t_next = time.monotonic()
        while True:
            n += 1
            if rounds and n > rounds:
                break
            if STOP_FILE.exists():
                print("检测到 .stop，退出")
                break

            t0 = time.monotonic()
            try:
                votes, raw = self.read_once(dry_run=dry_run,
                                            save=(self.save_mode == "all"))
                bad = self.suspect_fields(votes)
                if bad and self.save_mode == "need" and self.last_images:
                    a, b = self.last_images
                    self.save_shot(a, "suspect_top")
                    self.save_shot(b, "suspect_bottom")
                    print("  ~ 已留档可疑轮截图")
                print("[%s] %s   用时 %.1fs%s" % (
                    datetime.now().strftime("%H:%M:%S"),
                    "  ".join("%s=%s" % (HEADERS.get(k, k), v) for k, v in
                              [("total", votes.get("total"))] +
                              [(k, votes.get(k)) for k in ORDER]),
                    time.monotonic() - t0,
                    ("  ！可疑 " + ",".join(bad)) if bad else ""))
                self.append_csv(votes)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print("[%s] 本轮失败: %s" % (datetime.now().strftime("%H:%M:%S"), e))

            if dry_run:
                break
            if rounds and n >= rounds:
                break
            t_next += self.interval
            left = t_next - time.monotonic()
            if left > 0:
                time.sleep(left)
            else:
                t_next = time.monotonic()

    def append_csv(self, v):
        prev = read_last_csv()
        total = v.get("total")
        d_total = (total - prev["total"]) if (prev and total and prev.get("total")) else None
        d_sum = None
        if prev:
            diffs = [v[k] - prev[k] for k in ORDER if v.get(k) and prev.get(k)]
            if diffs:
                d_sum = sum(diffs)
        share = None
        if d_total and d_sum is not None and d_total > 0:
            share = round(d_sum / d_total * 100, 2)

        new = not CSV_PATH.exists()
        with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "总票数", HEADERS["r1l"], HEADERS["r1r"],
                            HEADERS["r2l"], HEADERS["r2r"],
                            "Δ总", "四人Δ合计", "四人Δ占比%"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        total, v.get("r1l"), v.get("r1r"), v.get("r2l"), v.get("r2r"),
                        d_total, d_sum, share])


def read_last_csv():
    if not CSV_PATH.exists():
        return None
    try:
        with CSV_PATH.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return None
        last = rows[-1]
        keys = ["total"] + ORDER
        out = {}
        for i, k in enumerate(keys):
            try:
                out[k] = int(last[i + 1])
            except (ValueError, IndexError):
                out[k] = None
        return out
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--rounds", type=int, default=0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save", choices=["none", "need", "all"], default="need",
                    help="截图留档：none 不存 / need 只在读数可疑时存（默认）/ all 每轮都存")
    args = ap.parse_args()

    cfg = load_config()
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    s = Scraper(cfg, args.interval, args.save)
    if not args.dry_run:
        s.dev.wake()

    if args.dry_run:
        votes, raw = s.read_once(dry_run=True, save=True)
        print("\n=== 顶部截图解析 ===")
        print(json.dumps(raw["top"], ensure_ascii=False))
        print("\n=== 滚动后截图解析 ===")
        print(json.dumps(raw["bottom"], ensure_ascii=False))
        print("\n=== 合并结果 ===")
        print(json.dumps(votes, ensure_ascii=False, indent=2))
        print("\n截图保存在 shots/（裁切后 JPEG）")
        return

    rounds = 1 if args.once else args.rounds
    s.run(rounds=rounds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as e:
        print("启动失败:", e)
        sys.exit(1)
