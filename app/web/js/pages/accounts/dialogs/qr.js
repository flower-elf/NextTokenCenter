// 授权链接二维码：只输出 SVG，按链接缓存。两个添加账号弹窗共用同一种扫码表现。
import { unsafeSVG } from '../../../core/dom.js';
import { qrcode } from '../../../../vendor/qrcode-generator-2.0.4.mjs';

// 缓存 SVG 文本，不缓存 unsafeSVG 的指令对象：指令对象可能带渲染状态。
const cache = new Map();

/** 链接太长或二维码生成失败时返回空串，由调用方隐藏二维码区域。 */
export function qrMarkup(text) {
  const value = String(text || '');
  if (!value) return '';
  let svg = cache.get(value);
  if (svg === undefined) {
    svg = '';
    try {
      const qr = qrcode(0, 'M');
      qr.addData(value);
      qr.make();
      svg = qr.createSvgTag({ cellSize: 4, margin: 8, scalable: true, alt: '授权链接二维码' });
    } catch (err) {
      svg = '';
    }
    if (cache.size > 8) cache.clear();
    cache.set(value, svg);
  }
  return svg ? unsafeSVG(svg) : '';
}
