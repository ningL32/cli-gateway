"""cli-gateway 命令行入口：replay / logs / run。

用法：
  cli-gateway run -- <command...>      执行并落盘
  cli-gateway replay <log-path>        回放一次调用
  cli-gateway logs [--dir DIR]         列出日志
"""

from __future__ import annotations

import argparse
import json
import platform
import sys

from . import DEFAULT_LOG_DIR, list_logs, replay, run
from .doctor import doctor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli-gateway", description="统一 CLI 进程调用网关")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一条命令并全量落盘")
    # 注意：不能叫 "cmd"——会与 subparsers 的 dest 同名冲突，覆盖子命令名导致 run 分支永不执行
    p_run.add_argument("payload", nargs=argparse.REMAINDER, help="要执行的命令（-- 后的全部参数）")
    p_run.add_argument("--timeout", type=float, default=None, help="超时秒数")
    p_run.add_argument("--dir", default=None, help="日志目录")

    p_replay = sub.add_parser("replay", help="回放一次已记录的调用")
    p_replay.add_argument("log_path", help="日志文件路径")

    p_logs = sub.add_parser("logs", help="列出全部调用日志")
    p_logs.add_argument("--dir", default=None, help="日志目录")

    p_doctor = sub.add_parser("doctor", help="体检本机编码 CLI：安装形态/版本/调用配方")
    p_doctor.add_argument("--only", nargs="+", metavar="CMD", help="只查指定命令（默认内置 9 个编码 CLI）")
    p_doctor.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p_doctor.add_argument("--no-probe", action="store_true", help="静态快查：只看 PATH 形态，不实跑版本探测")
    p_doctor.add_argument("--timeout", type=float, default=20.0, help="单命令探测超时秒数")

    args = parser.parse_args(argv)

    # 打印子进程 tail 前兜底：裸 CP936 控制台编不出 UTF-8 字符时 replace，
    # 防 UnicodeEncodeError 直接崩掉 CLI（不改变 stdout 的编码，只放宽错误策略）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass

    try:
        if args.cmd == "run":
            # 切片写法：payload 为空列表时 args.payload[0] 会 IndexError，parser.error 永不可达
            cmd = args.payload[1:] if args.payload[:1] == ["--"] else args.payload
            if not cmd:
                parser.error("run 需要一个命令")
            res = run(cmd, timeout=args.timeout, log_dir=args.dir)
            print(f"exit={res.exit_code} duration={res.duration_ms}ms log={res.log_path}")
            if res.stdout_tail:
                print(res.stdout_tail, end="")
            if res.stderr_tail:
                print(res.stderr_tail, end="", file=sys.stderr)
            # 透传真实退出码（124 超时 / 127 未找到等），调用方脚本可直接判断
            return 0 if res.success else (res.exit_code or 1)

        if args.cmd == "replay":
            print(replay(args.log_path))
            return 0

        if args.cmd == "logs":
            for p in list_logs(args.dir):
                print(p)
            return 0

        if args.cmd == "doctor":
            rows = doctor(commands=args.only, probe=not args.no_probe, timeout=args.timeout)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"cli-gateway doctor  {platform.platform()}  python {sys.version.split()[0]}")
                for r in rows:
                    mark = "✅" if r["ok"] else "❌"
                    ver = r["version"] or "-"
                    dur = f"  {r['duration_ms']}ms" if r["duration_ms"] is not None else ""
                    print(f"  {mark} {r['name']:<10} {r['form']}  |  {ver}{dur}")
                    if r["form"] == "npm shim（需 cmd /c 包装）":
                        print(f"      └─ 网关调用配方：cmd /c {r['cmd']} ...（Git Bash 写 //c）")
                ok_n = sum(1 for r in rows if r["ok"])
                print(f"\n{ok_n}/{len(rows)} 可用；探测日志已落盘 {DEFAULT_LOG_DIR}")
            return 0 if all(r["ok"] for r in rows) else 1
    except (OSError, ValueError, TypeError) as exc:
        # 库层抛出的参数/环境错误给单行提示，不甩裸 traceback
        print(f"cli-gateway: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
