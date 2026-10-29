import type { Category } from './catalog';

// Stable presentation keys are separate from category slugs owned by the catalog.
export const CATEGORY_ENTRIES = [
  { key: 'home', name: '居家日用' },
  { key: 'food', name: '滋味零食' },
  { key: 'fresh', name: '食品生鲜' },
  { key: 'bags', name: '首饰箱包' },
  { key: 'digital', name: '数码家电' },
  { key: 'beauty', name: '全球美妆' },
  { key: 'kitchen', name: '厨房用品' },
  { key: 'kids', name: '母婴儿童' },
];

export function categoryKey(value?: string): string {
  return CATEGORY_ENTRIES.some(item => item.key === value) ? value! : 'all';
}

export function categoryOptions(categories: Category[]) {
  return [{ key: 'all', name: '全部好物', slug: '', available: true },
    ...CATEGORY_ENTRIES.map(item => {
      // Never turn an unconfigured category into an unfiltered product request.
      const matches = categories.filter(category => category.name === item.name &&
        category.slug.trim() && category.slug.length <= 80);
      return { ...item, slug: matches.length === 1 ? matches[0].slug : '', available: matches.length === 1 };
    })];
}
