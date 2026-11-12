import { requestMemberCatalog } from './member-session';
import { downloadHomeBannerImage } from './catalog-images';
import { CATEGORY_ENTRIES } from './category-navigation';

export interface HomeBanner {
  slot: number; title: string; imageUrl: string; sourcePath: string; targetCategory: string;
}

export async function getHomeBanners(): Promise<HomeBanner[]> {
  const data = await requestMemberCatalog('/home-banners');
  const categories = ['', ...CATEGORY_ENTRIES.map(entry => entry.key)];
  if (!Array.isArray(data) || data.length > 5 || !data.every(row => row &&
      Number.isInteger(row.slot) && row.slot >= 1 && row.slot <= 5 &&
      typeof row.title === 'string' && row.title.trim().length > 0 && row.title.length <= 80 &&
      typeof row.image_url === 'string' && categories.includes(row.target_category)) ||
      new Set(data.map(row => row.slot)).size !== data.length) throw new Error('轮播素材暂不可用');
  const images = await Promise.all(data.map(async row => ({slot: row.slot, title: row.title,
    sourcePath: row.image_url, targetCategory: row.target_category,
    imageUrl: await downloadHomeBannerImage(row.image_url, row.slot)})));
  return images.filter(row => Boolean(row.imageUrl));
}
