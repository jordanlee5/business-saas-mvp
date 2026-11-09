// No credentials or product data in storage. Listeners scrub visible and hidden views.
export type AccessState = 'checking' | 'login' | 'eligible' | 'disabled' | 'expired' | 'pending' | 'unconfirmed' | 'unavailable';
let epoch = 0;
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
