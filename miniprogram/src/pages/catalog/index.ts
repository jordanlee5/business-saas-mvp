import { getCategories, getProducts, publicImageUrl } from '../../services/catalog';
import type { Category, Product } from '../../services/catalog';

interface ProductCard extends Product { imageUrl: string }
interface CategoryTab extends Category { selected: boolean }

let generation = 0;

Page({
  data: {
    categories: [{ name: '全部', slug: '', selected: true }] as CategoryTab[],
    activeSlug: '',
    items: [] as ProductCard[],
    total: 0,
    page: 0,
    loading: true,
    moreLoading: false,
    error: '',
    hasMore: false,
  },
  onLoad() { this.refresh(true); },
  onUnload() { generation += 1; },
  onPullDownRefresh() {
    this.refresh(true).finally(() => wx.stopPullDownRefresh());
  },
  onRetry() { this.refresh(true); },
  async refresh(loadCategories: boolean) {
    const current = ++generation;
    this.setData({ loading: true, moreLoading: false, error: '', items: [], page: 0, total: 0, hasMore: false });
    try {
      if (loadCategories) {
        const categories = await getCategories();
        if (current !== generation) return;
        const tabs = [{ name: '全部', slug: '' }, ...categories].map((item) => ({
          ...item, selected: item.slug === this.data.activeSlug,
        }));
        this.setData({ categories: tabs });
      }
      const result = await getProducts(this.data.activeSlug, 1);
      if (current !== generation) return;
      this.setData({
        items: result.items.map((item: Product) => ({
          ...item, imageUrl: publicImageUrl(item.main_image_url),
        })),
        page: 1,
        total: result.total,
        hasMore: result.items.length < result.total,
      });
    } catch (error) {
      if (current === generation) this.setData({ error: String(error instanceof Error ? error.message : '加载失败') });
    } finally {
      if (current === generation) this.setData({ loading: false });
    }
  },
  onCategoryTap(event: { currentTarget: { dataset: { slug: string } } }) {
    const slug = event.currentTarget.dataset.slug;
    if (slug === this.data.activeSlug) return;
    this.setData({
      activeSlug: slug,
      categories: this.data.categories.map((item: CategoryTab) => ({
        ...item, selected: item.slug === slug,
      })),
    });
    this.refresh(false);
  },
  async onLoadMore() {
    if (this.data.loading || this.data.moreLoading || !this.data.hasMore) return;
    const current = generation;
    const nextPage = this.data.page + 1;
    this.setData({ moreLoading: true, error: '' });
    try {
      const result = await getProducts(this.data.activeSlug, nextPage);
      if (current !== generation) return;
      const items = this.data.items.concat(result.items.map((item: Product) => ({
        ...item, imageUrl: publicImageUrl(item.main_image_url),
      })));
      this.setData({ items, page: nextPage, hasMore: items.length < result.total });
    } catch (error) {
      if (current === generation) this.setData({ error: String(error instanceof Error ? error.message : '加载失败') });
    } finally {
      if (current === generation) this.setData({ moreLoading: false });
    }
  },
});
