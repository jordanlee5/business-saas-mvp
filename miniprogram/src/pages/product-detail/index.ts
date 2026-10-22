import { getProductDetail, ProductNotFoundError, productImageUrl } from '../../services/catalog';
import type { ProductDetail, ProductSku } from '../../services/catalog';
import { addCartLine } from '../../services/cart';

interface DisplaySku extends ProductSku { stockLabel: string; stockClass: string; selected: boolean }
interface DisplayImage { url: string; alt: string }

let generation = 0;

Page({
  data: {
    id: '',
    loading: true,
    notFound: false,
    error: '',
    product: null as ProductDetail | null,
    gallery: [] as DisplayImage[],
    detailImages: [] as DisplayImage[],
    skus: [] as DisplaySku[],
    selectedSkuCode: '',
    selectedSku: null as DisplaySku | null,
    cartNotice: '',
    cartError: '',
  },
  onLoad(options: { id?: string }) {
    const id = options.id || '';
    this.setData({ id });
    this.refresh();
  },
  onUnload() { generation += 1; },
  onPullDownRefresh() {
    this.refresh().finally(() => wx.stopPullDownRefresh());
  },
  onRetry() { this.refresh(); },
  onBack() {
    if (getCurrentPages().length > 1) wx.navigateBack({ delta: 1 });
    else wx.reLaunch({ url: '/pages/catalog/index' });
  },
  onSkuTap(event: { currentTarget: { dataset: { code: string } } }) {
    const code = event.currentTarget.dataset.code;
    const selected = this.data.skus.find((sku: DisplaySku) => sku.sku_code === code);
    if (!selected) return;
    this.setData({
      selectedSkuCode: code,
      selectedSku: { ...selected, selected: true },
      skus: this.data.skus.map((sku: DisplaySku) => ({
        ...sku, selected: sku.sku_code === code,
      })),
      cartNotice: '',
      cartError: '',
    });
  },
  onAddToCart() {
    if (!this.data.product || !this.data.selectedSku) {
      this.setData({ cartError: '请先选择商品规格', cartNotice: '' });
      return;
    }
    try {
      addCartLine(this.data.product, this.data.selectedSku);
      this.setData({ cartNotice: '已加入购物车', cartError: '' });
    } catch (error) {
      this.setData({ cartError: error instanceof Error ? error.message : '加入购物车失败', cartNotice: '' });
    }
  },
  onCartTap() { wx.reLaunch({ url: '/pages/cart/index' }); },
  async refresh() {
    const current = ++generation;
    this.setData({ loading: true, error: '', notFound: false, product: null, gallery: [], detailImages: [], skus: [], selectedSku: null, cartNotice: '', cartError: '' });
    try {
      const product = await getProductDetail(this.data.id);
      if (current !== generation) return;
      const images = product.images.map((item) => ({
        role: item.role,
        url: productImageUrl(item.url, product.product_public_id),
        alt: item.alt_text || product.name,
      })).filter((item) => item.url);
      const gallery = images.filter((item) => item.role !== 'detail');
      const detailImages = images.filter((item) => item.role === 'detail');
      const skus = product.skus.map((sku: ProductSku) => ({
        ...sku,
        stockLabel: sku.stock_status === 'OUT_OF_STOCK' ? '暂时无货' :
          sku.stock_status === 'LOW_STOCK' ? '库存紧张' : '有货',
        stockClass: sku.stock_status === 'OUT_OF_STOCK' ? 'out' :
          sku.stock_status === 'LOW_STOCK' ? 'low' : 'available',
        selected: sku.sku_code === this.data.selectedSkuCode,
      }));
      const selectedSku = skus.find((sku: DisplaySku) => sku.selected) || null;
      this.setData({
        product,
        gallery,
        detailImages,
        skus,
        selectedSku,
        selectedSkuCode: selectedSku ? selectedSku.sku_code : '',
      });
    } catch (error) {
      if (current !== generation) return;
      if (error instanceof ProductNotFoundError) this.setData({ notFound: true, selectedSkuCode: '' });
      else this.setData({ error: String(error instanceof Error ? error.message : '加载失败') });
    } finally {
      if (current === generation) this.setData({ loading: false });
    }
  },
});
