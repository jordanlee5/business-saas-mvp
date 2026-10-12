import { API_ORIGIN } from '../config';

export interface Category { name: string; slug: string }
export interface Product {
  product_public_id: string;
  name: string;
  subtitle: string | null;
  category_slug: string;
  min_points_price: string;
  in_stock: boolean;
  main_image_url: string | null;
}
export interface ProductPage { items: Product[]; page: number; page_size: number; total: number }
export interface ProductImage { role: 'main' | 'carousel' | 'detail'; url: string; alt_text: string | null }
export interface ProductSku {
  sku_code: string; name: string; points_price: string; in_stock: boolean;
  stock_status: 'IN_STOCK' | 'LOW_STOCK' | 'OUT_OF_STOCK';
}
export interface ProductDetail extends Product {
  description: string | null;
  images: ProductImage[];
  skus: ProductSku[];
}

export class ProductNotFoundError extends Error {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function request(path: string, detail = false): Promise<unknown> {
  return new Promise((resolve, reject) => wx.request({
    url: API_ORIGIN + path,
    method: 'GET',
    timeout: 8000,
    header: { Accept: 'application/json' },
    success(response) {
      if (response.statusCode !== 200) {
        if (detail && response.statusCode === 404) {
          reject(new ProductNotFoundError('商品已下架或不存在'));
          return;
        }
        reject(new Error('目录服务暂不可用'));
        return;
      }
      resolve(response.data);
    },
    fail() { reject(new Error('网络连接失败，请稍后重试')); },
  }));
}

export async function getCategories(): Promise<Category[]> {
  const data = await request('/api/miniprogram/v1/categories');
  if (!Array.isArray(data) || !data.every((item) =>
    isRecord(item) && typeof item.name === 'string' && typeof item.slug === 'string')) {
    throw new Error('分类数据暂不可用');
  }
  return data as Category[];
}

export async function getProducts(category: string, page: number): Promise<ProductPage> {
  const query = `?page=${page}&page_size=20` +
    (category ? `&category=${encodeURIComponent(category)}` : '');
  const data = await request('/api/miniprogram/v1/products' + query);
  if (!isRecord(data) || !Array.isArray(data.items) ||
      data.page !== page || data.page_size !== 20 ||
      typeof data.total !== 'number' || !Number.isSafeInteger(data.total) || data.total < 0 ||
      !data.items.every((item) => isRecord(item) &&
        typeof item.product_public_id === 'string' && typeof item.name === 'string' &&
        typeof item.min_points_price === 'string' && typeof item.in_stock === 'boolean' &&
        (item.subtitle === null || typeof item.subtitle === 'string') &&
        (item.main_image_url === null || typeof item.main_image_url === 'string'))) {
    throw new Error('商品数据暂不可用');
  }
  return data as unknown as ProductPage;
}

export function publicImageUrl(path: string | null): string {
  if (!path || !/^\/uploads\/mall_products\/[A-Za-z0-9_./-]+$/.test(path) ||
      path.includes('..') || path.includes('//')) return '';
  return API_ORIGIN + path;
}

export function productImageUrl(path: string | null, id: string): string {
  if (!path || !path.startsWith(`/uploads/mall_products/${id}/`)) return '';
  return publicImageUrl(path);
}

export async function getProductDetail(id: string): Promise<ProductDetail> {
  if (!/^[A-Z0-9-]{1,32}$/.test(id)) throw new ProductNotFoundError('商品编号无效');
  const data = await request('/api/miniprogram/v1/products/' + encodeURIComponent(id), true);
  if (!isRecord(data) || data.product_public_id !== id || typeof data.name !== 'string' ||
      (data.subtitle !== null && typeof data.subtitle !== 'string') ||
      (data.description !== null && typeof data.description !== 'string') ||
      typeof data.category_slug !== 'string' || typeof data.min_points_price !== 'string' ||
      typeof data.in_stock !== 'boolean' ||
      (data.main_image_url !== null && typeof data.main_image_url !== 'string') ||
      !Array.isArray(data.images) || !data.images.every((image) => isRecord(image) &&
        ['main', 'carousel', 'detail'].includes(String(image.role)) &&
        typeof image.url === 'string' &&
        (image.alt_text === null || typeof image.alt_text === 'string')) ||
      !Array.isArray(data.skus) || data.skus.length === 0 || !data.skus.every((sku) =>
        isRecord(sku) && typeof sku.sku_code === 'string' && typeof sku.name === 'string' &&
        typeof sku.points_price === 'string' && typeof sku.in_stock === 'boolean' &&
        ['IN_STOCK', 'LOW_STOCK', 'OUT_OF_STOCK'].includes(String(sku.stock_status)) &&
        sku.in_stock === (sku.stock_status !== 'OUT_OF_STOCK'))) {
    throw new Error('商品详情数据暂不可用');
  }
  return data as unknown as ProductDetail;
}
