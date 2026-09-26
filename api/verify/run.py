"""验收编排：在本业务场景下依次完成代码测试、构建检查与 API/HTTP 冒烟。

退出码：0 = 全部通过；1 = 任一步骤失败。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def banner(msg: str) -> None:
    print(f"\n[verify] ======== {msg} ========", flush=True)


def run_step(name: str, argv: list[str]) -> bool:
    banner(name)
    rc = subprocess.call(argv, cwd=ROOT)
    print(f"[verify] 步骤「{name}」退出码={rc}", flush=True)
    return rc == 0


def main() -> int:
    results: dict[str, bool] = {}

    results["代码测试 (pytest)"] = run_step(
        "代码测试：级联失效 / 幂等裁决 / 并发竞争 / 重启持久化",
        [sys.executable, "-m", "pytest", "-q", "tests"],
    )

    results["构建检查"] = run_step(
        "构建检查：字节码编译 / 应用装配 / 前端资产 / 编排文件",
        [sys.executable, "-m", "verify.buildcheck"],
    )

    api_base = os.environ.get("API_BASE_URL", "http://api:8000")
    web_base = os.environ.get("WEB_BASE_URL") or None
    banner(f"API/HTTP 冒烟：目标 {api_base}" + (f"，页面 {web_base}" if web_base else ""))
    from verify.smoke import run_smoke

    results["API/HTTP 冒烟"] = run_smoke(api_base, web_base)

    banner("验收结果汇总")
    for name, ok in results.items():
        print(f"[verify]   {name}: {'PASS' if ok else 'FAIL'}", flush=True)
    overall = all(results.values())
    print(f"[verify] 总体结果: {'PASS' if overall else 'FAIL'}", flush=True)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
