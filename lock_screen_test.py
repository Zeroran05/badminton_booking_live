"""Wait 30 seconds, then run one safe Dry Run while the Mac is locked."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WAIT_SECONDS = 30


def main() -> int:
    print("锁屏测试将在 30 秒后开始。现在请按 Control + Command + Q 锁屏。", flush=True)
    for remaining in range(WAIT_SECONDS, 0, -5):
        print(f"距离 Dry Run 还有 {remaining} 秒……", flush=True)
        time.sleep(min(5, remaining))

    print("开始锁屏 Dry Run：只扫描，不会提交预约。", flush=True)
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--run-now", "--once"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        print("锁屏测试成功：门户访问、登录和可用性扫描均已完成。", flush=True)
    else:
        print(
            f"锁屏测试结束，程序返回码为 {result.returncode}；请解锁后查看 logs/。",
            flush=True,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
