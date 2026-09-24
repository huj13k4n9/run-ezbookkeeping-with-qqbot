#!/bin/sh
# 容器入口：幂等准备 pi 的运行环境，然后交给 CMD。
#
# 为什么扩展要在启动时装、而不是烤进镜像：
#   agent dir（PI_CODING_AGENT_DIR）是**从宿主机绑定挂载**进来的，
#   镜像里装好的东西会被挂载点遮住。装到挂载目录里才能持久化。
#
# 这一步失败不拦启动 —— 没有 Langfuse 追踪，记账照样能用。
set -eu

AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
mkdir -p "$AGENT_DIR"

if [ -n "${PI_CODING_AGENT_SESSION_DIR:-}" ]; then
    mkdir -p "$PI_CODING_AGENT_SESSION_DIR"
fi

# 也建一下 cwd 里的占位，避免 pi 找不到工作目录
[ -d /app/agent ] || mkdir -p /app/agent

# 绑定挂载**单个文件**时，如果宿主机上那个文件不存在，Docker 会自作主张
# 建一个**同名目录**挂进来。pi 去 JSON.parse 一个目录就会失败，
# 表现为「自定义模型不生效」「Langfuse 没 trace」——很难查。这里直接报出来。
for f in "$AGENT_DIR/models.json" "$AGENT_DIR/langfuse.json"; do
    if [ -d "$f" ]; then
        printf '
[entrypoint] ===== 配置错误 =====
'
        printf '[entrypoint] %s 是个目录，不是文件。
' "$f"
        printf '[entrypoint] 原因：宿主机上缺这个文件，Docker 自动建了目录。
'
        printf '[entrypoint] 修复：
'
        printf '  rm -rf pi-config/%s
' "$(basename "$f")"
        printf '  cp pi-config/%s.example pi-config/%s
' "$(basename "$f")" "$(basename "$f")"
        printf '[entrypoint] =====================

'
    fi
done

if [ -n "${PI_EXTENSIONS:-}" ]; then
    for src in $PI_EXTENSIONS; do
        # 镜像构建时已经预装过，这里先问一下 pi list，能省掉一次网络往返。
        # grep 失败（输出格式变了 / pi list 不可用）则照样装 —— 宁可多做不可少做。
        if pi list 2>/dev/null | grep -qF "$src"; then
            printf '[entrypoint] pi 扩展已就绪: %s\n' "$src"
            continue
        fi
        printf '[entrypoint] 安装 pi 扩展: %s\n' "$src"
        if ! PI_CODING_AGENT_DIR="$AGENT_DIR" pi install "$src"; then
            printf '[entrypoint] 警告: %s 安装失败（离线或 npm 不可用），继续启动\n' "$src"
        fi
    done
fi

exec "$@"
