#!/bin/sh
# 准备 pi 的两个配置文件（幂等：已存在就跳过）。
#
# 为什么必须存在这两个文件：
#   compose 把它们是**按文件**挂进容器的。如果宿主机上不存在，
#   Docker 会自作主张建一个**同名目录**挂进去，pi 解析 JSON 就会失败
#   （表现为「自定义模型不生效」或「Langfuse 没有 trace」）。
#
# 生成的是**无害的空配置**：models.json 没有任何自定义端点，
# langfuse.json 的 key 为空（插件会干净地关闭追踪）。
# 要启用就去改这两个文件，然后 docker compose restart。
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/pi-config"
mkdir -p "$DIR"

created=0

if [ ! -e "$DIR/models.json" ]; then
    cat > "$DIR/models.json" <<'JSON'
{
  "providers": {}

  // 想用自己的中转 / 网关（自定义 LLM base URL）？
  // 把上面那行换成下面这样，端点、密钥、模型名就都在这一个文件里了：
  //
  // "defaultModel": "anthropic/claude-sonnet-4-5",
  // "providers": {
  //   "anthropic": { "baseUrl": "https://your-relay.example.com", "apiKey": "sk-..." }
  // }
  //
  // 注意：只支持 // 行注释和尾随逗号，写 /* */ 块注释会让 pi 静默忽略整个文件。
  // 改完：docker compose restart qqbot
}
JSON
    chmod 600 "$DIR/models.json"   # 可能含密钥
    echo "[init-config] 已生成 pi-config/models.json（空配置 + 带注释的模板）"
    created=1
fi

if [ ! -e "$DIR/langfuse.json" ]; then
    cat > "$DIR/langfuse.json" <<'JSON'
{
  "publicKey": "",
  "secretKey": "",
  "baseUrl": "https://cloud.langfuse.com"
}
JSON
    chmod 600 "$DIR/langfuse.json"
    echo "[init-config] 已生成 pi-config/langfuse.json（key 为空 = 追踪关闭）"
    created=1
fi

# 万一之前已经被 Docker 建成目录了，帮用户指出来（这里不自动删）
for f in models.json langfuse.json; do
    if [ -d "$DIR/$f" ]; then
        echo "[init-config] 错误：$DIR/$f 是目录，应该是文件。" >&2
        echo "[init-config] 修复：rm -rf '$DIR/$f' 然后重跑本脚本。" >&2
        exit 1
    fi
done

if [ "$created" = "0" ]; then
    echo "[init-config] pi-config/ 下的配置文件已存在，未改动"
fi

echo "[init-config] 模板参考：pi-config/models.json.example / pi-config/langfuse.json.example"
