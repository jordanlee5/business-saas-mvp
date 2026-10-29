import { getCategories, getProducts, publicImageUrl, type Product } from './catalog';
import { CATEGORY_ENTRIES, categoryKey, categoryOptions } from './category-navigation';

interface ProductCard extends Product { imageUrl: string }
type Tap = { currentTarget: { dataset: { key?: string; id?: string; src?: string } } };
const cards = (items: Product[]) => items.map(item => ({ ...item, imageUrl: publicImageUrl(item.main_image_url) }));
const columns = (items: ProductCard[]) => [0, 1].map(index => ({
  key: String(index), items: items.filter((_, i) => i % 2 === index),
}));

export function catalogPage(isCategoryPage: boolean) {
  let generation = 0;
  return {
    data: {
      entries: CATEGORY_ENTRIES, categories: categoryOptions([]), categoriesReady: false,
      activeKey: 'all', activeName: '全部好物', draftQuery: '', activeQuery: '', searchError: '',
      items: [] as ProductCard[], columns: columns([]), total: 0, page: 0, scrollTop: 0,
      loading: true, moreLoading: false, error: '', hasMore: false,
    },
    onLoad(options: { category?: string } = {}) {
      const key = isCategoryPage ? categoryKey(options.category) : 'all';
      this.setData({ activeKey: key, activeName: CATEGORY_ENTRIES.find(item => item.key === key)?.name || '全部好物' });
      this.refresh(true);
    },
    onUnload() { generation += 1; },
    onPullDownRefresh() { this.refresh(true).finally(() => wx.stopPullDownRefresh()); },
    onRetry() { this.refresh(true); },
    onProductsScroll(event: { detail: { scrollTop: number } }) { this.setData({ scrollTop: event.detail.scrollTop }); },
    async refresh(loadCategories: boolean): Promise<void> {
      const current = ++generation;
      const key = this.data.activeKey, query = this.data.activeQuery;
      this.setData({ loading: true, moreLoading: false, error: '', items: [], columns: columns([]), page: 0, total: 0, hasMore: false, scrollTop: 0 });
      try {
        if (isCategoryPage && (loadCategories || !this.data.categoriesReady)) {
          const categories = categoryOptions(await getCategories());
          if (current !== generation) return;
          this.setData({ categories, categoriesReady: true });
        }
        const selected = this.data.categories.find(item => item.key === key);
        if (isCategoryPage && (!selected || !selected.available)) return;
        const result = await getProducts(isCategoryPage ? selected!.slug : '', 1, query);
        if (current !== generation) return;
        const items = cards(result.items);
        this.setData({ items, columns: columns(items), page: 1, total: result.total, hasMore: items.length < result.total });
      } catch (error) {
        if (current === generation) this.setData({ error: error instanceof Error ? error.message : '加载失败，请稍后重试' });
      } finally {
        if (current === generation) this.setData({ loading: false });
      }
    },
    onSearchInput(event: { detail: { value: string } }) { this.setData({ draftQuery: event.detail.value, searchError: '' }); },
    onSearchSubmit() {
      const query = this.data.draftQuery.trim();
      if (query.length > 80) { this.setData({ searchError: '搜索词不能超过80个字符' }); return; }
      this.setData({ draftQuery: query, activeQuery: query, searchError: '' });
      this.refresh(false);
    },
    onClearSearch() {
      const hadSearch = Boolean(this.data.activeQuery);
      this.setData({ draftQuery: '', activeQuery: '', searchError: '' });
      if (hadSearch) this.refresh(false);
    },
    onCategoryTap(event: Tap) {
      const key = event.currentTarget.dataset.key;
      if (!this.data.categories.some(item => item.key === key)) return;
      if (!isCategoryPage) {
        wx.reLaunch({ url: '/pages/categories/index?category=' + encodeURIComponent(key!) });
        return;
      }
      if (key === this.data.activeKey) return;
      this.setData({ activeKey: key, activeName: this.data.categories.find(item => item.key === key)!.name,
        draftQuery: this.data.activeQuery, searchError: '' });
      this.refresh(false);
    },
    onProductTap(event: Tap) {
      const id = event.currentTarget.dataset.id || '';
      if (/^[A-Z0-9-]{1,32}$/.test(id)) wx.navigateTo({ url: '/pages/product-detail/index?id=' + encodeURIComponent(id) });
    },
    onImageError(event: Tap) {
      const {id, src} = event.currentTarget.dataset;
      const items = this.data.items.map(item => item.product_public_id === id && item.imageUrl === src ? {...item, imageUrl: ''} : item);
      this.setData({ items, columns: columns(items) });
    },
    onCategoriesTap() { wx.reLaunch({ url: '/pages/categories/index' }); },
    onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
    onCartTap() { wx.reLaunch({ url: '/pages/cart/index' }); },
    onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
    async onLoadMore(): Promise<void> {
      if (this.data.loading || this.data.moreLoading || !this.data.hasMore) return;
      const current = generation, nextPage = this.data.page + 1;
      const selected = this.data.categories.find(item => item.key === this.data.activeKey);
      if (isCategoryPage && (!selected || !selected.available)) return;
      this.setData({ moreLoading: true, error: '' });
      try {
        const result = await getProducts(isCategoryPage ? selected!.slug : '', nextPage, this.data.activeQuery);
        if (current !== generation) return;
        const items = this.data.items.concat(cards(result.items));
        this.setData({ items, columns: columns(items), total: result.total, page: nextPage, hasMore: items.length < result.total });
      } catch (error) {
        if (current === generation) this.setData({ error: error instanceof Error ? error.message : '加载失败，请稍后重试' });
      } finally {
        if (current === generation) this.setData({ moreLoading: false });
      }
    },
  };
}
