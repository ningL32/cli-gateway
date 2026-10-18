"""cli-gateway 命令行入口：replay / logs / run。

用法：
  cli-gateway run -- <command...>      执行并落盘
  cli-gateway replay <log-path>        回放一次调用
  cli-gateway logs [--dir DIR]         列出日志
"""

from __future__ import annotations

import argparse
import sys

from . import list_logs, replay, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli-gateway", description="统一 CLI 进程调用网关")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一条命令并全量落盘")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令（-- 后的全部参数）")
    p_run.add_argument("--timeout", type=float, default=None, help="超时秒数")
    p_run.add_argument("--dir", default=None, help="日志目录")

    p_replay = sub.add_parser("replay", help="回放一次已记录的调用")
    p_replay.add_argument("log_path", help="日志文件路径")

    p_logs = sub.add_parser("logs", help="列出全部调用日志")
    p_logs.add_argument("--dir", default=None, help="日志目录")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        cmd = args.cmd if args.cmd[0] != "--" else args.cmd[1:]
        if not cmd:
            parser.error("run 需要一个命令")
        res = run(cmd, timeout=args.timeout, log_dir=args.dir)
        print(f"exit={res.exit_code} duration={res.duration_ms}ms log={res.log_path}")
        if res.stdout_tail:
            print(res.stdout_tail, end="")
        if res.stderr_tail:
            print(res.stderr_tail, end="", file=sys.stderr)
        return 0 if res.success else 1

    if args.cmd == "replay":
        print(replay(args.log_path))
        return 0

    if args.cmd == "logs":
        for p in list_logs(args.dir):
            print(p)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
