#!/usr/bin/env node
/**
 * 生图页 DOM 冒烟（M7）：不开浏览器、不引第三方库，只做"关键元素与关键逻辑存在"的契约检查。
 * 用法：node scripts/ui_smoke_image.js
 *
 * 第四轮 W1：新增 `web/image_payload.js` 的用例（混合选择 / 全选 / 无色商品 / 不勾颜色），
 * 直接把纯函数 require 进来跑，验的是"真的拼对参数"，不是源码里出现过某个词。
 */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/image.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web/image.js"), "utf8");
const adminJs = fs.readFileSync(path.join(root, "web/image_admin.js"), "utf8");
const payload = require(path.join(root, "web", "image_payload.js"));

const failures = [];
function expect(name, ok) {
  if (!ok) failures.push(name);
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}`);
}

// 1) 页面结构
for (const id of ["login-panel", "workbench", "admin-panel", "product-rows",
                  "composition-select", "scene-style-select", "count-input",
                  "slot-count-input", "mode-select", "platform-select",
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
// 第四轮 P2-12：空的 scope-note 占位删掉，页面上不残留空段落
expect("无空 scope-note 占位", !html.includes('id="scope-note"'));
// 第四轮 P0-1：选色拼装脚本必须先于 image.js 加载，否则建任务时会缺颜色参数
expect("image_payload.js 先于 image.js 加载",
  html.indexOf("/image_payload.js") > 0 && html.indexOf("/image_payload.js") < html.indexOf("/image.js"));

// 2) 关键逻辑
expect("SSE 走 fetch+Authorization", js.includes("openStream") && !js.includes("new EventSource"));
expect("SSE 断线重连", js.includes("openStreamWithRetry"));
expect("独立登录态", js.includes("image_agent_token"));
expect("签名 URL 直出", js.includes("signed_url"));
expect("隐藏/显示工作台", js.includes("workbench"));
expect("管理面板手绘 SVG", adminJs.includes("createElementNS") && adminJs.includes("polyline"));
// 第三轮（v2）：图位数/出图张数分离、颜色多选、模式/双平台、iterate、部分成功可见
expect("图位数与出图张数分离", js.includes("slot_count") && js.includes("image_count"));
expect("按颜色建任务", js.includes("product_colors") || js.includes("all_colors"));
expect("双平台开关", js.includes("platform_variant"));
expect("iterate 入口", js.includes("attachIterateButton"));
expect("部分成功可见", js.includes("partial") && html.includes('id="create-hint"'));
// 第五轮 P0-3：失败/取消不套用「部分成功」文案，只显示输出/预期与 error_code
expect("失败不冒充部分成功", js.includes("data.partial && data.message"));
// 第六轮 P1-5：失败原因（error_code）优先，且不被「输出/预期」覆盖
expect("失败原因不被覆盖", js.includes("const outputNote")
  && js.includes("${data.error_code} · ${outputNote}"));
expect("批次号可恢复", js.includes("image_agent_batch"));
expect("页面版本 image-v2", html.includes('data-ui-build="image-v2"') &&
  js.includes('"image-v2"'));
// 第四轮 P0-1：旧的「全局 allColors」全有或全无逻辑必须彻底消失
expect("选色走纯函数", js.includes("buildColorPlan") && !js.includes("let allColors"));
// 第四轮 P1-10：iterate 的图位数取快照字段，不回落读输入框
expect("iterate 图位数取快照", js.includes("data.slot_count || data.image_count"));

// 3) 选色参数拼装（真跑纯函数）
const products = [
  { product_id: "A", colors: [{ color: "米色" }, { color: "黑色" }, { color: "藏蓝" }] },
  { product_id: "B", colors: [{ color: "红色" }, { color: "白色" }] },
  { product_id: "C", colors: [] },
  { product_id: "D", colors: [{ color: "" }] },
];
function plan(selected, chosen) {
  return payload.buildTaskPayload({
    selected,
    products,
    colorsMap: new Map(Object.entries(chosen || {})),
  });
}
function colorsFor(result, productId) {
  const hit = (result.product_colors || []).find((entry) => entry.product_id === productId);
  return hit ? hit.colors : null;
}
function same(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

// 混合选择：A 勾满、B 只勾一个
const mixed = plan(["A", "B"], { A: new Set(["米色", "黑色", "藏蓝"]), B: new Set(["红色"]) });
expect("混合选色：勾满的商品不被丢弃（P0-1 丢色）",
  mixed.all_colors === false && same(colorsFor(mixed, "A"), ["米色", "黑色", "藏蓝"]));
expect("混合选色：部分勾选的商品只出已选颜色", same(colorsFor(mixed, "B"), ["红色"]));

// 全选：每个商品的每个颜色都勾满
const full = plan(["A", "B"], {
  A: new Set(["米色", "黑色", "藏蓝"]),
  B: new Set(["红色", "白色"]),
});
expect("全部勾满 → all_colors=true 且不重复发 product_colors",
  full.all_colors === true && full.product_colors.length === 0 && full.color_count === 5);

// 无色商品：没有颜色维度 / 只有空串默认颜色的商品不出现在 product_colors
const plain = plan(["A", "C", "D"], { A: new Set(["米色"]) });
expect("无色商品不出现在 product_colors",
  colorsFor(plain, "C") === null && colorsFor(plain, "D") === null);
expect("无色商品不污染 all_colors 与其它商品的颜色",
  plain.all_colors === false && same(colorsFor(plain, "A"), ["米色"]));

// 不勾颜色：只用第一个颜色（旧逻辑在这里会被 all_colors 波及，多买颜色 → 资损）
const untouched = plan(["A", "B"], { A: new Set(["米色", "黑色", "藏蓝"]) });
expect("没勾颜色的商品只出第一个颜色（P0-1 资损）",
  untouched.all_colors === false &&
  same(colorsFor(untouched, "A"), ["米色", "黑色", "藏蓝"]) &&
  same(colorsFor(untouched, "B"), ["红色"]));

// 空串颜色标签：展示成「默认」，但绝不进 payload
expect("空串颜色展示为「默认」", payload.colorLabel("") === "默认" && payload.colorLabel("米色") === "米色");
expect("空串颜色绝不进 product_colors",
  plan(["D"], {}).product_colors.length === 0 && plan(["D"], {}).all_colors === false);

if (failures.length) {
  console.error(`\n${failures.length} 项失败`);
  process.exit(1);
}
console.log("\n生图页 DOM 冒烟通过");
