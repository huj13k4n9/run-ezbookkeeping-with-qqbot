# qqbot + pi agent 单容器镜像
FROM node:24-bookworm-slim

# pi 的版本固定住，避免某天 latest 变动把线上搞挂
ARG PI_VERSION=0.87.1
ARG TZ=Asia/Shanghai
ARG DEBIAN_FRONTEND=noninteractive

# 系统依赖：
#   python3 / python3-venv / python3-pip : qqbot 本体
#   curl / jq                            : ebktools.sh 的硬依赖（脚本启动会自检）
#   ripgrep                              : pi 的 grep 工具
#   git                                  : pi 官方镜像也装（部分内置流程会用到）
#   tzdata                               : zoneinfo 需要（Debian slim 不带完整时区库）
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      jq \
      ripgrep \
      python3 \
      python3-venv \
      python3-pip \
      tzdata \
 && rm -rf /var/lib/apt/lists/*
# pi（官方推荐的装法：--ignore-scripts）
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force

ENV TZ=${TZ} \
    HOME=/home/qqbot \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Python 依赖单独一层：只改业务代码时不必重装
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# 应用代码（agent / .agents 由 compose 从宿主机挂进来，
# 这样改 AGENTS.md 或换 ebktools 版本都不用重建镜像）
COPY qqbot/ ./qqbot/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/check_pi_json.js /usr/local/bin/check-pi-json.js

# pi 的配置目录。里面只有扩展声明（settings.json）和预装的扩展包；
# 真正的配置 models.json / langfuse.json 由 compose **逐文件**挂进来，
# 所以密钥不会留在镜像层，而且改完重启容器即可，不用重建。

# 非 root 运行。
#
# 注意：/home/qqbot/.pi 必须在**镜像里就存在且属于 qqbot**。
# compose 里虽然会把 PI_CODING_AGENT_DIR 指到挂载目录，
# 但没设那个变量时 pi 会回到 ~/.pi/agent，先建好更稳。
RUN useradd --create-home --uid 10001 qqbot \
 && mkdir -p /home/qqbot/.pi/agent /app/data /app/agent /app/pi-config \
 && chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/check-pi-json.js \
 && chown -R qqbot:qqbot /home/qqbot /app

USER qqbot

# 把 PI_EXTENSIONS 里的 pi 扩展预先装进镜像，好处：
#   * 启动时不依赖网络（VPS 离线也能起来）
#   * 启动更快
# 入口脚本仍会做幂等兜底（镜像外追加的扩展、或这一层被清理时）。
ARG PI_EXTENSIONS="npm:@langfuse/pi-observability-plugin"
RUN for src in ${PI_EXTENSIONS}; do \
      PI_CODING_AGENT_DIR=/app/pi-config pi install "$src" \
      || echo "[build] 警告: $src 预装失败（离线？），改为启动时安装"; \
    done

# 入口会幂等安装 PI_EXTENSIONS 里的 pi 扩展，再 exec 到 CMD
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "scripts/run_bot.py"]
