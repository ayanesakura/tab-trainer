// 首屏渲染前写好 data-theme 和 data-palette，免得暗色系统下先闪一下浅色。要用普通 <script>（不是模块）放在 <head> 里同步加载。
// 完整的主题逻辑（切换、跟随系统变化）在 /static/ui.js，规则和这里一致：data-theme 总是实际生效的 light / dark。
(() => {
  let t = null;
  try { t = localStorage.getItem('tt-theme'); } catch { /* 无痕模式等读不了 localStorage */ }
  if (t !== 'light' && t !== 'dark') t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = t;
  // 配色（data-palette）：默认靛蓝；规则和 ui.js 的 getPalette 一致
  let p = null;
  try { p = localStorage.getItem('tt-palette'); } catch { /* 同上 */ }
  document.documentElement.dataset.palette = ['walnut', 'pine', 'cinnabar'].includes(p) ? p : 'indigo';
})();
