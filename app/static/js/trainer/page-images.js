// 原谱页面图片（共用的缓存）：按 view.pages 加载，每张加载好或失败时发 document 的 tt-images 事件。
// 侧栏的原谱截图（bar-card.js）和原图弹窗（orig.js）都从这里取，各自监听 tt-images 重画。

export const images = { list: [], src: [] };
export function loadImages(pages) {
  if (images.src.join() === pages.join()) return;
  images.src = pages.slice();
  images.list = pages.map(src => {
    const im = new Image();
    im.onload = () => { im.ok = true; document.dispatchEvent(new Event('tt-images')); };
    im.onerror = () => { im.bad = true; document.dispatchEvent(new Event('tt-images')); };
    im.src = src;
    return im;
  });
}
