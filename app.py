#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.py —— 双击即用的总入口（也是 exe 的打包入口）

启动后自动做四件事：
  1. 检查 adb 与手机连接
  2. 在后台起一个本地看板服务
  3. 用默认浏览器打开看板
  4. 开始每分钟一轮的票数采集

停止：关掉这个黑窗口，或按 Ctrl+C
"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 输出改为行缓冲 + UTF-8：否则把输出重定向到文件或管道时会整块卡住看不到进度
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass

# 打包成 exe 后，数据要落在 exe 旁边，不能落在临时解压目录
if getattr(sys, "frozen", False):
    os.environ.setdefault("VOTE_TRACKER_HOME", str(Path(sys.executable).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve_dashboard
import vote_scraper


BANNER = r"""
  ___  ___  ___  ___    _____ ___   ___ _  __
 | _ \| __|/ __|/ __|  |_   _| _ \ / _ \ |/ /
 |  _/| _|| (__| (__     | | |   /|  _/   <
 |_|  |___|\___|\___|    |_| |_|_\|_| |_|\_\

       和平营地 · 人气投票票数采集器
"""


def check_env():
    """返回 True 表示可以直接开跑"""
    adb = vote_scraper.ADB
    if not Path(adb).is_file() and adb == "adb":
        print("\n[X] 找不到 adb。")
        print("    请先安装 Android SDK Platform-Tools，或把 platform-tools 文件夹")
        print("    放到本程序同级目录。下载地址：")
        print("    https://developer.android.com/tools/releases/platform-tools")
        return False

    print("[1/4] adb 位置: %s" % adb)
    try:
        dev = vote_scraper.Adb()
        serial = dev.ensure_device()
        w, h = dev.size()
        print("      手机已连接: %s  分辨率 %dx%d" % (serial, w, h))
        return True
    except Exception as e:
        print("\n[X] 手机没连上: %s" % e)
        print()
        print("    按顺序检查：")
        print("      1. 手机 设置 → 关于手机 → 连点「版本号」7 次")
        print("      2. 开发者选项 → 打开「USB 调试」")
        print("      3. 数据线插电脑，手机弹窗点「允许」")
        print("      4. 跑一下 adb devices，设备状态必须是 device")
        print()
        print("    华为/荣耀设备如果 adb devices 一直为空，见 README 的「驱动修复」一节。")
        return False


def main():
    print(BANNER)
    if not check_env():
        print("\n按回车键退出…")
        try:
            input()
        except EOFError:
            time.sleep(5)
        return 1

    port = int(os.environ.get("DASHBOARD_PORT", "8770"))
    print("[2/4] 看板服务: http://127.0.0.1:%d/" % port)
    try:
        httpd = serve_dashboard.make_server(port)
    except OSError:
        print("      端口 %d 被占用，换个端口：set DASHBOARD_PORT=8771" % port)
        print("\n按回车键退出…")
        try:
            input()
        except EOFError:
            time.sleep(5)
        return 1
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print("[3/4] 正在打开浏览器…")
    webbrowser.open("http://127.0.0.1:%d/" % port)

    cfg = vote_scraper.load_config()
    interval = float(os.environ.get("SCRAPE_INTERVAL", "60"))
    save_mode = os.environ.get("SAVE_SHOTS", "all")

    print("[4/4] 开始采集，每轮间隔 %.0f 秒。截图留档模式: %s" % (interval, save_mode))
    print("      数据写入: %s" % vote_scraper.CSV_PATH)
    print("      截图写入: %s" % vote_scraper.SHOT_DIR)
    print("      ──── 按 Ctrl+C 或关掉本窗口即可停止 ────\n")

    try:
        s = vote_scraper.Scraper(cfg, interval, save_mode)
        s.dev.wake()
        s.run()
    except KeyboardInterrupt:
        print("\n已停止采集。数据都保留在 votes.csv 里。")
    except Exception as e:
        print("\n出错了: %s" % e)
        print("\n按回车键退出…")
        try:
            input()
        except EOFError:
            time.sleep(5)
        return 1
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
