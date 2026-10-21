import { getProductDetail, ProductNotFoundError } from './catalog';
import type { ProductDetail, ProductSku } from './catalog';

const STORAGE_KEY = 'mall_cart_v1';
const MAX_LINES = 20;
const MAX_QUANTITY = 99;

export interface CartLine {
  product_public_id: string;
  sku_code: string;
  quantity: number;
}

export interface CartDisplayLine extends CartLine {
  key: string;
  product_name: string;
  sku_name: string;
  points_price: string;
  line_points: string;
  available: boolean;
  status: string;
}

export class CartError extends Error {}

function validLine(value: unknown): value is CartLine {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const line = value as Record<string, unknown>;
  return typeof line.product_public_id === 'string' &&
    /^[A-Z0-9-]{1,32}$/.test(line.product_public_id) &&
    typeof line.sku_code === 'string' && line.sku_code.length > 0 &&
    line.sku_code.length <= 100 && !/[\x00-\x1f\x7f]/.test(line.sku_code) &&
    typeof line.quantity === 'number' && Number.isInteger(line.quantity) && line.quantity >= 1 &&
    (line.quantity as number) <= MAX_QUANTITY &&
    Object.keys(line).sort().join(',') === 'product_public_id,quantity,sku_code';
}

export function readCart(): CartLine[] {
  let stored: unknown;
  try { stored = wx.getStorageSync(STORAGE_KEY); }
  catch (_) { throw new CartError('无法读取本机购物车，请稍后重试'); }
  if (stored === '' || stored === undefined || stored === null) return [];
  if (!Array.isArray(stored) || stored.length > MAX_LINES ||
      !stored.every(validLine)) throw new CartError('本机购物车数据异常，请勿继续操作');
  const keys = stored.map((line: CartLine) => line.product_public_id + '\0' + line.sku_code);
  if (new Set(keys).size !== keys.length) throw new CartError('本机购物车数据异常，请勿继续操作');
  return stored.map((line: CartLine) => ({ ...line }));
}

function saveCart(lines: CartLine[]): void {
  try { wx.setStorageSync(STORAGE_KEY, lines); }
  catch (_) { throw new CartError('保存购物车失败，请稍后重试'); }
}

export function addCartLine(product: ProductDetail, sku: ProductSku): CartLine[] {
  if (!product || !sku || !validLine({
    product_public_id: product.product_public_id, sku_code: sku.sku_code, quantity: 1,
  }) || !product.skus.some((item) => item.sku_code === sku.sku_code) ||
      !sku.in_stock || sku.stock_status === 'OUT_OF_STOCK') {
    throw new CartError('请先选择有货的商品规格');
  }
  const lines = readCart();
  const existing = lines.find((line) =>
    line.product_public_id === product.product_public_id && line.sku_code === sku.sku_code);
  if (existing) {
    if (existing.quantity >= MAX_QUANTITY) throw new CartError('单款规格最多加入 99 件');
    existing.quantity += 1;
  } else {
    if (lines.length >= MAX_LINES) throw new CartError('购物车最多保留 20 款规格');
    lines.push({ product_public_id: product.product_public_id, sku_code: sku.sku_code, quantity: 1 });
  }
  saveCart(lines);
  return lines;
}

export function setCartQuantity(id: string, skuCode: string, quantity: number): CartLine[] {
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > MAX_QUANTITY)
    throw new CartError('数量需在 1 至 99 件之间');
  const lines = readCart();
  const line = lines.find((item) => item.product_public_id === id && item.sku_code === skuCode);
  if (!line) throw new CartError('购物车中未找到该规格');
  line.quantity = quantity;
  saveCart(lines);
  return lines;
}

export function removeCartLine(id: string, skuCode: string): CartLine[] {
  const lines = readCart();
  const next = lines.filter((item) => item.product_public_id !== id || item.sku_code !== skuCode);
  if (next.length === lines.length) throw new CartError('购物车中未找到该规格');
  saveCart(next);
  return next;
}

function pointsCents(value: string): number {
  if (!/^\d{1,10}\.\d{2}$/.test(value)) throw new CartError('商品积分价暂不可用');
  const [whole, fraction] = value.split('.');
  const cents = Number(whole) * 100 + Number(fraction);
  if (!Number.isSafeInteger(cents)) throw new CartError('商品积分价暂不可用');
  return cents;
}

function formatCents(cents: number): string {
  return (Math.floor(cents / 100)).toString() + '.' + String(cents % 100).padStart(2, '0');
}

export async function loadCartDisplay(lines: CartLine[]): Promise<{
  items: CartDisplayLine[]; estimate: string; hasUnavailable: boolean;
}> {
  if (!Array.isArray(lines) || lines.length > MAX_LINES || !lines.every(validLine))
    throw new CartError('本机购物车数据异常，请勿继续操作');
  const details = new Map<string, ProductDetail | ProductNotFoundError>();
  const ids = [...new Set(lines.map((line) => line.product_public_id))];
  for (let start = 0; start < ids.length; start += 4) {
    await Promise.all(ids.slice(start, start + 4).map(async (id) => {
      try { details.set(id, await getProductDetail(id)); }
      catch (error) {
        if (!(error instanceof ProductNotFoundError)) throw error;
        details.set(id, error);
      }
    }));
  }
  const items: CartDisplayLine[] = [];
  let totalCents = 0;
  let hasUnavailable = false;
  for (const line of lines) {
    const detail = details.get(line.product_public_id);
    const key = line.product_public_id + ':' + line.sku_code;
    if (detail instanceof ProductNotFoundError) {
      hasUnavailable = true;
      items.push({ ...line, key, product_name: '商品已下架', sku_name: line.sku_code,
        points_price: '', line_points: '', available: false, status: '已下架或不可见' });
      continue;
    }
    if (!detail) throw new CartError('商品详情暂不可用');
    const product = detail;
    const sku = product.skus.find((item) => item.sku_code === line.sku_code);
    if (!sku) {
      hasUnavailable = true;
      items.push({ ...line, key, product_name: product.name, sku_name: line.sku_code,
        points_price: '', line_points: '', available: false, status: '规格已不可见' });
      continue;
    }
    const available = sku.in_stock && sku.stock_status !== 'OUT_OF_STOCK';
    if (!available) hasUnavailable = true;
    const cents = pointsCents(sku.points_price);
    const lineCents = cents * line.quantity;
    if (!Number.isSafeInteger(lineCents) || !Number.isSafeInteger(totalCents + lineCents))
      throw new CartError('购物车积分合计暂不可用');
    if (available) totalCents += lineCents;
    items.push({ ...line, key, product_name: product.name, sku_name: sku.name,
      points_price: sku.points_price, line_points: formatCents(lineCents),
      available, status: available ? (sku.stock_status === 'LOW_STOCK' ? '库存紧张' : '有货') : '暂时无货' });
  }
  return { items, estimate: formatCents(totalCents), hasUnavailable };
}
