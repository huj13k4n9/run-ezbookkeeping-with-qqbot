#!/usr/bin/env node
// 用 pi 自己的解析规则检查 pi 的 JSON 配置能不能读懂。
//
// 为什么要单独验：models.json / langfuse.json 解析失败时，pi 只是
// **静默忽略整个文件** —— 不报错、不回退提示，表现就是「配置没生效」。
// 实测过：写一个 /* */ 块注释就会踩这个坑。
//
// 用法：check-pi-json.js <file> [file...]
// 退出码：有文件解析失败 = 1，全部正常 = 0

const fs = require("node:fs");

// 与 pi 的 dist/utils/json.js 里 stripJsonComments 保持一致：
// 只处理 `//` 行注释和尾随逗号，字符串字面量原样保留。
// **不处理 /* */ 块注释** —— pi 也不处理，所以这里也不处理。
function stripJsonComments(input) {
  return input
    .replace(/"(?:\\.|[^"\\])*"|\/\/[^\n]*/g, (m) => (m[0] === '"' ? m : ""))
    .replace(/"(?:\\.|[^"\\])*"|,(\s*[}\]])/g, (m, tail) => (tail ?? (m[0] === '"' ? m : "")));
}

let bad = 0;

for (const file of process.argv.slice(2)) {
  if (!fs.existsSync(file)) continue;

  const raw = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
  try {
    JSON.parse(stripJsonComments(raw));
  } catch (err) {
    bad += 1;
    console.error(`[entrypoint] 警告: ${file} 解析失败；pi 会静默忽略整个文件（等于没配）`);
    console.error(`[entrypoint]   ${err.message}`);
    console.error("[entrypoint]   只支持 // 行注释和尾随逗号，不支持 /* */ 块注释");
  }
}

process.exit(bad ? 1 : 0);
