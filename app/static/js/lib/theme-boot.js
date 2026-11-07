// 首屏渲染前写好 data-theme，免得暗色系统下先闪一下浅色。要用普通 <script>（不是模块）放在 <head> 里同步加载。
// 完整的主题逻辑（切换、跟随系统变化）在 /static/ui.js，规则和这里一致：data-theme 总是实际生效的 light / dark。
(() => {
  let t = null;
  try { t = localStorage.getItem('tt-theme'); } catch { /* 无痕模式等读不了 localStorage */ }
  if (t !== 'light' && t !== 'dark') t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = t;
})();
