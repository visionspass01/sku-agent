/* ============================================================
 * 建任务参数拼装（纯函数：无 DOM、无网络、可被 Node 直接 require）
 * ------------------------------------------------------------
 * 为什么单独抽一个文件（第四轮 P0-1）：
 *   过去 image.js 用「全局 allColors 开关 + 每个商品 return」的方式拼颜色，
 *   混合选择时是「全有或全无」，两个方向都错：
 *     1) 资损：某商品一个颜色都没勾（只想出第一个颜色）时不会把 allColors 置 false，
 *        于是 all_colors=true 把它的**全部颜色**都出了图 → 多买颜色；
 *     2) 丢色：某商品把颜色勾满、另一个商品只勾了部分时，勾满的商品被整体丢弃，
 *        中台只给它出第一个颜色 → 用户已选的颜色被静默丢掉。
 *   现在改成「逐商品显式产出 product_colors」；all_colors 只有在
 *   「每个选中商品的所有可选颜色都勾满」时才为 true。
 *
 * 颜色口径（与状态层一致）：一律保存**原始颜色名**，空串代表
 *   「商品没有颜色维度 / 默认主图」。空串不能出现在 product_colors 里
 *   （后端会把未知颜色判成 IMG_INPUT_INVALID），所以只在展示层显示成「默认」。
 * ============================================================ */
(function (root, factory) {
  const api = factory();
  if (api && typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ImagePayload = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** 商品可选颜色的原始值（含空串=「默认」），去重保序。 */
  function rawColorsOf(product) {
    const list = product && Array.isArray(product.colors) ? product.colors : [];
    const out = [];
    list.forEach((entry) => {
      const value = entry && typeof entry === "object" ? entry.color : entry;
      const name = value == null ? "" : String(value);
      if (out.indexOf(name) < 0) out.push(name);
    });
    return out;
  }

  /** 可以提交给中台的颜色名（去掉空串）；空串代表商品没有颜色维度。 */
  function namedColorsOf(product) {
    return rawColorsOf(product).filter((name) => name !== "");
  }

  /** 展示用标签：空串显示「默认」，其它原样。 */
  function colorLabel(name) {
    const raw = name == null ? "" : String(name);
    return raw === "" ? "默认" : raw;
  }

  function chosenColorsOf(colorsMap, productId) {
    if (!colorsMap) return [];
    let picked;
    if (typeof colorsMap.get === "function") picked = colorsMap.get(productId);
    else picked = colorsMap[productId];
    if (!picked) return [];
    if (typeof picked === "string") return [picked];
    return Array.from(picked);
  }

  /**
   * 逐商品拼出建任务的颜色参数。
   * @param {{selected?: (string[]|Set<string>|Iterable<string>),
   *          products?: object[],
   *          colorsMap?: (Map<string, Set<string>>|Record<string, Set<string>>)}} input
   * @returns {{all_colors: boolean, product_colors: {product_id: string, colors: string[]}[],
   *            color_count: number}}
   */
  function buildTaskPayload(input) {
    const opts = input || {};
    const selected = Array.from(opts.selected || []);
    const products = Array.isArray(opts.products) ? opts.products : [];
    const known = new Map();
    products.forEach((item) => {
      if (item && item.product_id) known.set(String(item.product_id), item);
    });

    const productColors = [];
    let allColors = selected.length > 0;
    selected.forEach((rawId) => {
      const productId = String(rawId);
      const named = namedColorsOf(known.get(productId));
      if (!named.length) {
        // 没有颜色维度（或商品清单里查不到）：不出现在 product_colors，交给中台按主图处理
        return;
      }
      const chosen = chosenColorsOf(opts.colorsMap, productId);
      const picked = named.filter((name) => chosen.indexOf(name) >= 0);
      if (picked.length === 0) {
        // 一个颜色都没勾 = 只用第一个颜色（显式写出来，避免被 all_colors 波及而多买）
        productColors.push({ product_id: productId, colors: [named[0]] });
        allColors = false;
      } else if (picked.length < named.length) {
        productColors.push({ product_id: productId, colors: picked });
        allColors = false;
      } else {
        // 勾满：显式列出全部颜色，别的商品勾不勾都不影响它
        productColors.push({ product_id: productId, colors: named.slice() });
      }
    });

    if (!productColors.length) allColors = false;
    const colorCount = productColors.reduce((sum, entry) => sum + entry.colors.length, 0);
    return {
      all_colors: allColors,
      product_colors: allColors ? [] : productColors,
      color_count: colorCount,
    };
  }

  return {
    rawColorsOf,
    namedColorsOf,
    colorLabel,
    buildTaskPayload,
  };
});
