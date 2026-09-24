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

# 应用代码（agent / .agents / pi-config 由 compose 从宿主机挂进来，
# 这样改 AGENTS.md、换 ebktools 版本、配 models.json 都不用重建镜像）
COPY qqbot/ ./qqbot/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# 非 root 运行。
#
# 注意：/home/qqbot/.pi 必须在**镜像里就存在且属于 qqbot**。
# compose 里虽然会把 PI_CODING_AGENT_DIR 指到挂载目录，
# 但没设那个变量时 pi 会回到 ~/.pi/agent，先建好更稳。
RUN useradd --create-home --uid 10001 qqbot \
 && mkdir -p /home/qqbot/.pi/agent /app/data /app/agent /app/pi-config \
 && chmod +x /usr/local/bin/entrypoint.sh \
 && chown -R qqbot:qqbot /home/qqbot /app

USER qqbot

# 入口会幂等安装 PI_EXTENSIONS 里的 pi 扩展，再 exec 到 CMD
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "scripts/run_bot.py"]
