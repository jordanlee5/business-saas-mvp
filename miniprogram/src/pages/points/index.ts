import {
  getCurrentMemberPointsLedger, getCurrentMemberPointsSummary,
  getSignedInMember, hasMemberSession, type MemberPointsLedgerEntry,
} from '../../services/member-session';

let viewGeneration = 0;
type LedgerDisplay = {
  key: string; title: string; amount: string; direction: string; created: string;
};

function dateText(value: string): string {
  return value.replace('T', ' ').replace('+08:00', '');
}

function displayLedger(entry: MemberPointsLedgerEntry, index: number, page: number): LedgerDisplay {
  const titles = {
    GRANT: '积分到账', RESERVE: '商品兑换', RELEASE: '订单取消退回',
    CONSUME: '商品兑换', REFUND: '退款退回', EXPIRE: '积分到期', ADJUST: '积分调整',
  };
  const outgoing = entry.available_points_delta.startsWith('-');
  return {
    key: `${page}-${index}`, title: titles[entry.entry_type],
    amount: `${outgoing ? '' : '+'}${entry.available_points_delta}`,
    direction: outgoing ? 'outgoing' : 'incoming', created: dateText(entry.created_at),
  };
}

function emptyPoints() {
  return {
    loading: false, signedIn: false, hasSession: false,
    availablePoints: '', nearestExpiry: '',
    ledgerItems: [] as LedgerDisplay[], ledgerPage: 0, ledgerTotal: 0,
    ledgerHasMore: false, ledgerLoadingMore: false, error: '',
  };
}

Page({
  data: emptyPoints(),
  onShow() { this.refreshPoints(); },
  onHide() { viewGeneration += 1; this.setData(emptyPoints()); },
  onUnload() { viewGeneration += 1; this.setData(emptyPoints()); },
  async refreshPoints() {
    const current = ++viewGeneration;
    const hasSession = hasMemberSession();
    this.setData({ ...emptyPoints(), loading: hasSession, hasSession });
    if (!hasSession) return;
    try {
      const member = await getSignedInMember();
      if (current !== viewGeneration) return;
      if (!member) throw new Error('登录已过期，请重新登录');
      const summary = await getCurrentMemberPointsSummary();
      if (current !== viewGeneration) return;
      if (!summary) throw new Error('登录已过期，请重新登录');
      const ledger = await getCurrentMemberPointsLedger(1, true);
      if (current !== viewGeneration) return;
      if (!ledger) throw new Error('登录已过期，请重新登录');
      this.setData({
        signedIn: true, availablePoints: summary.available_points,
        nearestExpiry: summary.nearest_expires_at ? dateText(summary.nearest_expires_at) : '',
        ledgerItems: ledger.items.map((entry, index) => displayLedger(entry, index, 1)),
        ledgerPage: 1, ledgerTotal: ledger.total, ledgerHasMore: ledger.items.length < ledger.total,
      });
    } catch (error) {
      if (current === viewGeneration) this.setData({
        ...emptyPoints(), hasSession: hasMemberSession(),
        error: error instanceof Error ? error.message : '暂时无法读取积分，请稍后重试',
      });
    } finally {
      if (current === viewGeneration) this.setData({ loading: false });
    }
  },
  async onMoreLedgerTap() {
    if (!this.data.signedIn || !this.data.ledgerHasMore ||
        this.data.ledgerLoadingMore || this.data.loading) return;
    const current = viewGeneration;
    const page = this.data.ledgerPage + 1;
    this.setData({ ledgerLoadingMore: true, error: '' });
    try {
      const ledger = await getCurrentMemberPointsLedger(page, true);
      if (current !== viewGeneration) return;
      if (!ledger) throw new Error('登录已过期，请重新登录');
      const items = this.data.ledgerItems.concat(
        ledger.items.map((entry, index) => displayLedger(entry, index, page)));
      this.setData({
        ledgerItems: items, ledgerPage: page, ledgerTotal: ledger.total,
        ledgerHasMore: items.length < ledger.total,
      });
    } catch (error) {
      if (current === viewGeneration) this.setData({
        ...emptyPoints(), hasSession: hasMemberSession(),
        error: error instanceof Error ? error.message : '暂时无法读取积分，请稍后重试',
      });
    } finally {
      if (current === viewGeneration) this.setData({ ledgerLoadingMore: false });
    }
  },
  onRetryTap() { this.refreshPoints(); },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
  onCartTap() { wx.reLaunch({ url: '/pages/cart/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
