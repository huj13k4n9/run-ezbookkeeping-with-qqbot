#!/bin/sh
# ezBookkeeping API Tools（复刻增强版）—— 唯一入口。
#
# 真实实现在同目录的 ebktools.py（标准库，不依赖 jq / pip 包）。
# 这里只负责定位解释器并把参数原样透传，保持与官方脚本相同的调用形态：
#
#   sh ebktools.sh list
#   sh ebktools.sh help transactions-add
#   sh ebktools.sh [--tz-offset 480] transactions-add --type 3 ...
#   sh ebktools.sh --tz-offset 480 query --range this-month --amount-filter gt:10000
#   sh ebktools.sh --tz-offset 480 stats --range this-month
#   sh ebktools.sh modify --id <ID> --comment "改成晚饭" --dry-run --tz-offset 480

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
entry="$script_dir/ebktools.py"

if [ ! -f "$entry" ]; then
    echo "错误：找不到 $entry" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    py=python3
elif command -v python >/dev/null 2>&1; then
    py=python
elif [ -n "${PYTHON:-}" ]; then
    py="$PYTHON"
else
    echo "错误：找不到 python3 / python（ebktools 需要 Python 3.8+）" >&2
    exit 1
fi

# 只依赖标准库，但语法用了 3.8+ 的特性；先检查，免得报一堆看不懂的语法错。
if ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    echo "错误：ebktools 需要 Python 3.8+，当前是 $("$py" -V 2>&1)" >&2
    exit 1
fi

exec "$py" "$entry" "$@"
