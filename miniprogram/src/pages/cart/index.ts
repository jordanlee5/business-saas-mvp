import { loadCartDisplay, readCart, removeCartLine, setCartQuantity } from '../../services/cart';
import type { CartDisplayLine } from '../../services/cart';

let generation = 0;

Page({
  data: {
    items: [] as CartDisplayLine[],
    loading: true,
    error: '',
    actionError: '',
    estimate: '0.00',
    hasUnavailable: false,
  },
  onShow() { this.refresh(); },
  onHide() { generation += 1; },
  onUnload() { generation += 1; },
  onPullDownRefresh() { this.refresh().finally(() => wx.stopPullDownRefresh()); },
  onRetry() { this.refresh(); },
  async refresh() {
    const current = ++generation;
    this.setData({ loading: true, error: '', actionError: '', items: [], estimate: '0.00' });
    try {
      const display = await loadCartDisplay(readCart());
      if (current === generation) this.setData(display);
    } catch (error) {
      if (current === generation)
        this.setData({ error: error instanceof Error ? error.message : '购物车暂不可用' });
    } finally {
      if (current === generation) this.setData({ loading: false });
    }
  },
  onIncrease(event: { currentTarget: { dataset: { id: string; sku: string } } }) {
    this.changeQuantity(event.currentTarget.dataset.id, event.currentTarget.dataset.sku, 1);
  },
  onDecrease(event: { currentTarget: { dataset: { id: string; sku: string } } }) {
    this.changeQuantity(event.currentTarget.dataset.id, event.currentTarget.dataset.sku, -1);
  },
  changeQuantity(id: string, sku: string, delta: number) {
    const row = this.data.items.find((item: CartDisplayLine) =>
      item.product_public_id === id && item.sku_code === sku);
    if (!row) return;
    try { setCartQuantity(id, sku, row.quantity + delta); this.refresh(); }
    catch (error) {
      this.setData({ actionError: error instanceof Error ? error.message : '修改数量失败' });
    }
  },
  onRemove(event: { currentTarget: { dataset: { id: string; sku: string } } }) {
    const { id, sku } = event.currentTarget.dataset;
    try { removeCartLine(id, sku); this.refresh(); }
    catch (error) {
      this.setData({ actionError: error instanceof Error ? error.message : '移除失败' });
    }
  },
  onProductTap(event: { currentTarget: { dataset: { id: string } } }) {
    const id = event.currentTarget.dataset.id;
    if (/^[A-Z0-9-]{1,32}$/.test(id))
      wx.navigateTo({ url: '/pages/product-detail/index?id=' + encodeURIComponent(id) });
  },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
