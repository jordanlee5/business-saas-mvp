import { requestMemberCatalog } from './member-session';
import { downloadHomeEntryImage } from './catalog-images';
import { CATEGORY_ENTRIES } from './category-navigation';

export const defaultHomeEntries = () => CATEGORY_ENTRIES.map(item => ({...item, imageUrl: ''}));

export async function getHomeEntries() {
  const data = await requestMemberCatalog('/home-entries');
  if (!Array.isArray(data) || data.length !== CATEGORY_ENTRIES.length ||
      !CATEGORY_ENTRIES.every(entry => data.filter(row => row && row.key === entry.key &&
        (row.image_url === null || typeof row.image_url === 'string')).length === 1)) {
    throw new Error('首页素材暂不可用');
  }
  // Slot names/order/navigation stay local; the server only supplies image overrides.
  return Promise.all(CATEGORY_ENTRIES.map(async entry => ({...entry,
    imageUrl: await downloadHomeEntryImage(data.find(row => row.key === entry.key).image_url, entry.key),
  })));
}
