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

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function request(path: string): Promise<unknown> {
  return new Promise((resolve, reject) => wx.request({
    url: API_ORIGIN + path,
    method: 'GET',
    timeout: 8000,
    header: { Accept: 'application/json' },
    success(response) {
      if (response.statusCode !== 200) {
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
