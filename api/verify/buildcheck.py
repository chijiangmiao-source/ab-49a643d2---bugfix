"""构建检查：字节码编译、应用可装配、前端资产与编排文件完整有效。"""

from __future__ import annotations

import compileall
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 镜像内布局为 /app/{app,web,compose.yaml}；本地开发时仓库根是 ROOT 的上一级。
CANDIDATES = [ROOT, os.path.dirname(ROOT)]


def _find(*names: str) -> str | None:
    for base in CANDIDATES:
        path = os.path.join(base, *names)
        if os.path.exists(path):
            return path
    return None


def _check(name: str, ok: bool, info: str = "") -> bool:
    suffix = f" | {info}" if info and not ok else ""
    print(f"[buildcheck] {'PASS' if ok else 'FAIL'}: {name}{suffix}", flush=True)
    return ok


def main() -> int:
    ok = True
    for pkg in ("app", "verify", "tests"):
        ok &= _check(
            f"字节码编译 {pkg}",
            compileall.compile_dir(os.path.join(ROOT, pkg), quiet=1),
        )

    os.environ.setdefault(
        "DATABASE_PATH",
        os.path.join(tempfile.mkdtemp(prefix="cryocal-buildcheck-"), "check.db"),
    )
    try:
        import importlib

        asgi = importlib.import_module("app.asgi")
        ok &= _check("应用可装配 (import app.asgi)", hasattr(asgi, "app"))
    except Exception as exc:  # noqa: BLE001
        ok &= _check("应用可装配 (import app.asgi)", False, repr(exc))

    index = _find("web", "index.html")
    ok &= _check("web/index.html 存在", index is not None)
    if index:
        with open(index, encoding="utf-8") as fh:
            html = fh.read()
        ok &= _check(
            "index.html 引用 app.js 与 styles.css",
            "app.js" in html and "styles.css" in html,
        )
    ok &= _check("web/app.js 存在", _find("web", "app.js") is not None)

    nginx = _find("web", "nginx.conf")
    ok &= _check("web/nginx.conf 存在", nginx is not None)
    if nginx:
        with open(nginx, encoding="utf-8") as fh:
            conf = fh.read()
        ok &= _check(
            "nginx.conf 将 /api 代理到 api 服务",
            "proxy_pass" in conf and "api" in conf,
        )

    compose = _find("compose.yaml")
    ok &= _check("compose.yaml 存在", compose is not None)
    if compose:
        try:
            import yaml

            with open(compose, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            services = (doc or {}).get("services", {})
            ok &= _check(
                "compose 定义 api/web/verify 服务",
                all(s in services for s in ("api", "web", "verify")),
            )
            ok &= _check(
                "api 服务配置健康检查",
                "healthcheck" in services.get("api", {}),
            )
        except Exception as exc:  # noqa: BLE001
            ok &= _check("compose.yaml 可解析", False, repr(exc))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
