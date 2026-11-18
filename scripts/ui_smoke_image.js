#!/usr/bin/env node
/**
 * 生图页 DOM 冒烟（M7）：不开浏览器、不引第三方库，只做"关键元素与关键逻辑存在"的契约检查。
 * 用法：node scripts/ui_smoke_image.js
 */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/image.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web/image.js"), "utf8");
const adminJs = fs.readFileSync(path.join(root, "web/image_admin.js"), "utf8");

const failures = [];
function expect(name, ok) {
  if (!ok) failures.push(name);
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}`);
}

// 1) 页面结构
for (const id of ["login-panel", "workbench", "admin-panel", "product-rows",
                  "composition-select", "scene-style-select", "count-input",
                  "aspect-select", "prompt-input", "btn-create", "progress",
                  "gallery", "metric-cards", "image-chart",
                  "ref-source-select", "library-panel", "library-grid"]) {
  expect(`页面含 #${id}`, html.includes(`id="${id}"`));
}
// W3：上传入口在「素材库」面板（参考图三源之一）；商品清单本身仍然只能从中台选，不能上传
expect("素材库有上传入口", html.includes('id="library-file"') &&
  html.includes('id="btn-library-upload"') && html.includes('type="file"'));
expect("商品图不可上传", !html.includes('id="product-upload"') && !html.includes("product-file"));
expect("参考图来源三选一", ["mcp", "library", "pailitao"].every(
  (source) => html.includes(`value="${source}"`)));
expect("无内联事件", !/\son(click|change|input)=/.test(html));
expect("脚本外链（版本号由服务端注入）", html.includes("/image.js") && html.includes("/image_admin.js"));

// 2) 关键逻辑
expect("SSE 走 fetch+Authorization", js.includes("openStream") && !js.includes("new EventSource"));
expect("SSE 断线重连", js.includes("openStreamWithRetry"));
expect("独立登录态", js.includes("image_agent_token"));
expect("签名 URL 直出", js.includes("signed_url"));
expect("隐藏/显示工作台", js.includes("workbench"));
expect("管理面板手绘 SVG", adminJs.includes("createElementNS") && adminJs.includes("polyline"));

if (failures.length) {
  console.error(`\n${failures.length} 项失败`);
  process.exit(1);
}
console.log("\n生图页 DOM 冒烟通过");
