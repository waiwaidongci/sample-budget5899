#!/usr/bin/env bash
# 一键回归：语法编译 + 三组测试，全部使用 -I 隔离模式
set -euo pipefail
cd "$(dirname "$0")"
python3 -I -m py_compile clickaudit/*.py
python3 -I demo/make_demo.py
python3 -I tests/smoke_solver.py
python3 -I tests/test_bruteforce.py
python3 -I tests/test_end_to_end.py
echo "==== 全部回归通过 ===="
