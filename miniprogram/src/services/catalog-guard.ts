import { checkCatalogAccess, accessMessages } from './catalog-access';
import { onCatalogInvalidated, invalidateCatalog, type AccessState } from './catalog-state';
import { hasMemberSession } from './member-session';

// Every entry (including shared/deep links) starts closed, checks the server, then loads.
export function guardCatalogPage(definition: any): any {
  function close(page: any, state: AccessState) {
    page.onCatalogClear();
    page.setData({ catalogReady: false, accessState: state, accessMessage: accessMessages[state] });
  }
  async function enter(page: any) {
    const attempt = page.__accessAttempt = (page.__accessAttempt || 0) + 1;
    close(page, 'checking');
    try {
      const state = await checkCatalogAccess();
      if (!page.__visible || attempt !== page.__accessAttempt) return;
      page.setData({ catalogReady: state === 'eligible', accessState: state, accessMessage: accessMessages[state] });
      if (state === 'eligible') await page.refresh(true);
    } catch (_) {
      if (page.__visible && attempt === page.__accessAttempt) close(page, hasMemberSession() ? 'unavailable' : 'login');
    }
  }
  function leave(page: any) {
    const wasVisible = page.__visible;
    page.__visible = false; page.__accessAttempt = (page.__accessAttempt || 0) + 1;
    if (page.__accessTimer) clearInterval(page.__accessTimer);
    if (page.__unsubscribe) page.__unsubscribe();
    close(page, 'checking');
    // Release page images on navigation as well as app backgrounding; old downloads are fenced out.
    if (wasVisible) invalidateCatalog('checking', false);
  }
  return {
    ...definition,
    data: {...definition.data, catalogReady: false, accessState: 'checking', accessMessage: accessMessages.checking},
    onShow() {
      this.__visible = true;
      if (this.__unsubscribe) this.__unsubscribe();
      this.__unsubscribe = onCatalogInvalidated((state) => close(this, state));
      if (this.__accessTimer) clearInterval(this.__accessTimer);
      this.__accessTimer = setInterval(async () => {
        if (!this.__visible || !this.data.catalogReady) return;
        try { await checkCatalogAccess(); }
        catch (_) { if (this.__visible) close(this, hasMemberSession() ? 'unavailable' : 'login'); }
      }, 30000);
      return enter(this);
    },
    onHide() { leave(this); if (definition.onHide) definition.onHide.call(this); },
    onUnload() { leave(this); if (definition.onUnload) definition.onUnload.call(this); },
    onRetryAccess() { return enter(this); },
    onAccessProfile() { wx.reLaunch({url: '/pages/profile/index'}); },
    onAccessActivation() { wx.navigateTo({url: '/pages/activation/index'}); },
    onAccessOrders() { wx.navigateTo({url: '/pages/orders/index'}); },
    onAccessRights() { wx.navigateTo({url: '/pages/rights/index'}); },
  };
}
