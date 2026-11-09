import { guardCatalogPage } from '../../services/catalog-guard';
import { downloadCatalogImage, forgetCatalogImage } from '../../services/catalog-images';
import { getProductDetail, ProductNotFoundError, productImageUrl } from '../../services/catalog';
import type { ProductDetail, ProductSku } from '../../services/catalog';
import { addCartLine } from '../../services/cart';

interface DisplaySku extends ProductSku { stockLabel: string; stockClass: string; selected: boolean }
interface DisplayImage { key: string; url: string; source: string; alt: string; failed: boolean; retrying?: boolean }
type ImageEvent = { currentTarget: { dataset: { kind: string; index: number | string; key: string } } };

Page(guardCatalogPage({
  generation: 0,
  imageSequence: 0,
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
  },
  onCatalogClear() { this.generation += 1; this.setData({product:null, gallery:[], detailImages:[], skus:[], selectedSku:null, selectedSkuCode:'', cartNotice:'', cartError:'', error:'', loading:false}); },
  onUnload() { this.generation += 1; },
  onPullDownRefresh() {
    this.refresh().finally(() => wx.stopPullDownRefresh());
  },
  onRetry() { if (!this.data.loading) return this.refresh(); },
  onImageError(event: ImageEvent) { this.updateImage(event, false); },
  async onRetryImage(event: ImageEvent) {
    const {kind, key} = event.currentTarget.dataset, index = Number(event.currentTarget.dataset.index);
    if ((kind !== 'gallery' && kind !== 'detailImages') || !Number.isInteger(index)) return;
    const item = this.data[kind][index] as DisplayImage;
    if (!item || item.key !== key || !item.failed || item.retrying) return;
    const generation = this.generation, nextKey = `image-${++this.imageSequence}`;
    const update = (values: Partial<DisplayImage>) => this.setData({[kind]: this.data[kind].map((value: DisplayImage, i: number) => i === index ? {...value, ...values} : value)});
    update({key: nextKey, retrying: true});
    forgetCatalogImage(item.source);
    try {
      const url = await downloadCatalogImage(item.source, this.data.id);
      if (generation === this.generation && this.data[kind][index]?.key === nextKey) update({url, failed: !url, retrying: false});
    } catch (_) {
      if (generation === this.generation && this.data[kind][index]?.key === nextKey) update({retrying: false});
    }
  },
  updateImage(event: ImageEvent, retry: boolean) {
    const { kind, key } = event.currentTarget.dataset;
    const index = Number(event.currentTarget.dataset.index);
    if ((kind !== 'gallery' && kind !== 'detailImages') || !Number.isInteger(index) || index < 0) return;
    const items = this.data[kind] as DisplayImage[];
    const item = items[index];
    // Late events from an earlier refresh or image attempt must not hide a new image.
    if (!item || item.key !== key || item.failed !== retry) return;
    const changed = { ...item, failed: !retry,
      key: retry ? `image-${++this.imageSequence}` : item.key };
    this.setData({ [kind]: items.map((value, position) => position === index ? changed : value) });
  },
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
    const current = ++this.generation;
    this.setData({ loading: true, error: '', notFound: false, product: null, gallery: [], detailImages: [], skus: [], selectedSku: null, cartNotice: '', cartError: '' });
    try {
      const product = await getProductDetail(this.data.id);
      if (current !== this.generation) return;
      const images = (await Promise.all(product.images.map(async (item) => ({
        role: item.role,
        source: item.url,
        key: `image-${++this.imageSequence}`,
        failed: false,
        url: await downloadCatalogImage(item.url, product.product_public_id),
        alt: item.alt_text || product.name,
      })))).filter(item => item.url || product.images.some(source => source.role === item.role && productImageUrl(source.url, product.product_public_id)));
      if (current !== this.generation) return;
      images.forEach(item => { item.failed = !item.url; });
      const gallery = images.filter((item) => item.role === 'main')
        .concat(images.filter((item) => item.role === 'carousel'));
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
      if (current !== this.generation) return;
      if (error instanceof ProductNotFoundError) this.setData({ notFound: true, selectedSkuCode: '' });
      else this.setData({ error: String(error instanceof Error ? error.message : '加载失败') });
    } finally {
      if (current === this.generation) this.setData({ loading: false });
    }
  },
}));
