#!/bin/sh
# 容器入口：启动前做几项健壮性检查，然后交给 CMD。
#
# pi 的扩展在**构建期**就装进镜像了（见 Dockerfile），所以这里通常没事可做；
# 只有镜像外追加的扩展、或者镜像层被改动过时，才会兜底再装一次。
#
# 所有检查都只告警、不拦启动 —— 记账能不能用不取决于 Langfuse 或自定义端点。
set -eu

AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
mkdir -p "$AGENT_DIR"

if [ -n "${PI_CODING_AGENT_SESSION_DIR:-}" ]; then
    mkdir -p "$PI_CODING_AGENT_SESSION_DIR"
fi

# 也建一下 cwd 里的占位，避免 pi 找不到工作目录
[ -d /app/agent ] || mkdir -p /app/agent

# ---------------------------------------------------------------- 配置检查 1
# 绑定挂载**单个文件**时，如果宿主机上那个文件不存在，Docker 会自作主张
# 建一个**同名目录**挂进来。pi 去读一个目录会失败，表现为「自定义模型不生效」
# 「Langfuse 没 trace」——很难查。这里直接报到脸上。
for f in "$AGENT_DIR/models.json" "$AGENT_DIR/langfuse.json"; do
    if [ -d "$f" ]; then
        name="$(basename "$f")"
        printf '\n[entrypoint] ===== 配置错误 =====\n'
        printf '[entrypoint] %s 是个目录，不是文件。\n' "$f"
        printf '[entrypoint] 原因：宿主机上缺这个文件，Docker 自动建了目录。\n'
        printf '[entrypoint] 修复：\n'
        printf '  rm -rf pi-config/%s\n' "$name"
        printf '  cp pi-config/%s.example pi-config/%s\n' "$name" "$name"
        printf '[entrypoint] =====================\n\n'
    fi
done

# ---------------------------------------------------------------- 配置检查 2
# JSON 写错（比如用了 /* */ 块注释）时 pi 会**静默忽略整个文件**，不报错。
# 用 pi 相同的规则提前验一遍，把「配置没生效」这种玄学问题变成一行明确报错。
if command -v check-pi-json.js >/dev/null 2>&1; then
    check-pi-json.js "$AGENT_DIR/models.json" "$AGENT_DIR/langfuse.json" || true
fi

# ---------------------------------------------------------------- 扩展兜底
if [ -n "${PI_EXTENSIONS:-}" ]; then
    for src in $PI_EXTENSIONS; do
        # 构建期已经预装过；先问一下 pi list 能省掉一次网络往返。
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
