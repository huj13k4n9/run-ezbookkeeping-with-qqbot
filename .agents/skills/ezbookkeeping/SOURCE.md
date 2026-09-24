# 来源说明（vendored）

这个目录下的内容**不是本项目的代码**，是从上游仓库按原样拷进来的。

| 项 | 值 |
| --- | --- |
| 上游仓库 | <https://github.com/mayswind/ezbookkeeping> |
| 上游路径 | `skills/ezbookkeeping/` |
| 作者 | MaysWind |
| 许可 | MIT（见同目录 `LICENSE`） |
| 取用时间 | 2026-09-24，取 `main` 分支 |
| 修改 | **无**。仅由 `.gitattributes` 统一了换行符（仓库内 LF），内容与上游一致 |

## 为什么要 vendored

本项目把它作为 pi agent 的**执行层**：agent 通过
`scripts/ebktools.sh` 调用 ezBookkeeping 的 REST API 来记账和查账。

不通过 npm / git submodule 引入，是为了让部署只依赖仓库自身 ——
容器里不需要额外拉取步骤，也不受上游分支变动影响。

## 升级方式

```bash
# 覆盖这两个文件即可（保持目录结构不变）
curl -fsSL https://raw.githubusercontent.com/mayswind/ezbookkeeping/main/skills/ezbookkeeping/SKILL.md \
  -o .agents/skills/ezbookkeeping/SKILL.md
curl -fsSL https://raw.githubusercontent.com/mayswind/ezbookkeeping/main/skills/ezbookkeeping/scripts/ebktools.sh \
  -o .agents/skills/ezbookkeeping/scripts/ebktools.sh
```

升级后建议核对 `ebktools.sh` 里内嵌的 `API_CONFIGS`：

```bash
sh scripts/ebktools.sh list
sh scripts/ebktools.sh help transactions-add
```

参数名如果变了，`agent/AGENTS.md` 里那段调用模板需要同步
（金额单位、`--categoryId`、`--sourceAccountId` 等）。
