// No credentials or product data in storage. Listeners scrub visible and hidden views.
export type AccessState = 'checking' | 'changed' | 'login' | 'eligible' | 'disabled' | 'expired' | 'pending' | 'unconfirmed' | 'unavailable';
let epoch = 0;
let pricingContext = '';
let organizationCode = '';
export function currentPricingContext(): string {
  if (!catalogAllowed() || !pricingContext) throw new Error('请刷新商城资格和价格后再兑换');
  return pricingContext;
}
export function acceptPricingContext(context: string, code: string): void {
  if (!/^[a-f0-9]{64}$/.test(context) || !code) throw new Error('机构价格信息暂不可用');
  if (pricingContext && (context !== pricingContext || code !== organizationCode))
    invalidateCatalog('changed', code !== organizationCode);
  pricingContext = context;
  organizationCode = code;
}
export function assertPricingContext(context: unknown): void {
  if (!pricingContext || context !== pricingContext) {
    invalidateCatalog('changed', false);
    throw new Error('机构或价格已变化，请刷新商城后重新核对');
  }
}
let deadline = 0;
let cartStorageReady = true;
let timer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<(state: AccessState) => void>();
export const catalogEpoch = () => epoch;
export function onCatalogInvalidated(listener: (state: AccessState) => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function invalidateCatalog(state: AccessState = 'checking', clearCart = true): void {
  epoch += 1;
  deadline = 0;
  if (timer) clearTimeout(timer);
  if (clearCart) {
    pricingContext = ''; organizationCode = '';
    try { wx.setStorageSync('mall_cart_v1', []); cartStorageReady = true; }
    catch (_) { cartStorageReady = false; }
  }
  for (const listener of listeners) {
    try { listener(state); } catch (_) { /* A detached view must not block other cleanup. */ }
  }
}
export function assertCartStorageReady(): void {
  if (!cartStorageReady) throw new Error('本机购物车清理失败，请重新登录后重试');
}
export function allowCatalog(until: number): void {
  if (timer) clearTimeout(timer);
  deadline = until;
  timer = setTimeout(() => invalidateCatalog('expired'), Math.max(0, until - Date.now()));
}
export function catalogAllowed(): boolean {
  if (deadline > Date.now()) return true;
  if (deadline) invalidateCatalog('expired');
  return false;
}
