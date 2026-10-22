import {
  getCurrentMemberPointsGrants, getCurrentMemberPointsLedger, getCurrentMemberPointsSummary,
  getSignedInMember, hasMemberSession, type MemberPointsGrant, type MemberPointsLedgerEntry,
} from '../../services/member-session';

let viewGeneration = 0;
type GrantDisplay = {
  key: string;
  granted: string; available: string; reserved: string;
  activated: string; expires: string; status: string;
};
type LedgerDisplay = {
  key: string; type: string; available: string; reserved: string; created: string;
};

function dateText(value: string): string {
  return value.replace('T', ' ').replace('+08:00', '（北京时间）');
}

function displayGrant(grant: MemberPointsGrant, index: number, page: number): GrantDisplay {
  const statuses = {
    ACTIVE: '有效', EXHAUSTED: '已用尽', EXPIRED: '已到期', FROZEN: '已冻结',
  };
  return {
    key: `${page}-${index}`,
    granted: grant.granted_points, available: grant.available_points,
    reserved: grant.reserved_points, activated: dateText(grant.activated_at),
    expires: dateText(grant.expires_at), status: statuses[grant.status],
  };
}

function displayLedger(entry: MemberPointsLedgerEntry, index: number, page: number): LedgerDisplay {
  const types = {
    GRANT: '积分入账', RESERVE: '积分预占', RELEASE: '预占释放',
    CONSUME: '积分消费', REFUND: '积分退回', EXPIRE: '积分到期', ADJUST: '积分调整',
  };
  return {
    key: `${page}-${index}`, type: types[entry.entry_type],
    available: entry.available_points_delta, reserved: entry.reserved_points_delta,
    created: dateText(entry.created_at),
  };
}

Page({
  data: {
    loading: false,
    signedIn: false,
    hasSession: false,
    memberPublicId: '',
    availablePoints: '',
    reservedPoints: '',
    grantCount: 0,
    nearestExpiry: '',
    grantItems: [] as GrantDisplay[],
    grantPage: 0,
    grantTotal: 0,
    hasMore: false,
    loadingMore: false,
    ledgerItems: [] as LedgerDisplay[],
    ledgerPage: 0,
    ledgerTotal: 0,
    ledgerHasMore: false,
    ledgerLoadingMore: false,
    error: '',
  },
  onShow() { this.refreshPoints(); },
  onHide() { viewGeneration += 1; },
  onUnload() { viewGeneration += 1; },
  async refreshPoints() {
    const current = ++viewGeneration;
    const hasSession = hasMemberSession();
    this.setData({
      loading: hasSession, hasSession, signedIn: false, memberPublicId: '',
      availablePoints: '', reservedPoints: '', grantCount: 0, nearestExpiry: '',
      grantItems: [], grantPage: 0, grantTotal: 0, hasMore: false, loadingMore: false, error: '',
      ledgerItems: [], ledgerPage: 0, ledgerTotal: 0, ledgerHasMore: false, ledgerLoadingMore: false,
    });
    if (!hasSession) return;
    try {
      const member = await getSignedInMember();
      if (current !== viewGeneration) return;
      if (!member) throw new Error('会员会话已失效，请重新登录');
      const summary = await getCurrentMemberPointsSummary();
      if (current !== viewGeneration) return;
      if (!summary) throw new Error('会员会话已失效，请重新登录');
      const grants = await getCurrentMemberPointsGrants(1);
      if (current !== viewGeneration) return;
      if (!grants) throw new Error('会员会话已失效，请重新登录');
      const ledger = await getCurrentMemberPointsLedger(1);
      if (current !== viewGeneration) return;
      if (!ledger) throw new Error('会员会话已失效，请重新登录');
      this.setData({
        signedIn: true, memberPublicId: member.member_public_id,
        availablePoints: summary.available_points, reservedPoints: summary.reserved_points,
        grantCount: summary.grant_count,
        nearestExpiry: summary.nearest_expires_at
          ? dateText(summary.nearest_expires_at) : '',
        grantItems: grants.items.map((grant, index) => displayGrant(grant, index, 1)),
        grantPage: 1, grantTotal: grants.total,
        hasMore: grants.items.length < grants.total,
        ledgerItems: ledger.items.map((entry, index) => displayLedger(entry, index, 1)),
        ledgerPage: 1, ledgerTotal: ledger.total,
        ledgerHasMore: ledger.items.length < ledger.total,
      });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          signedIn: false, memberPublicId: '', availablePoints: '', reservedPoints: '',
          grantCount: 0, nearestExpiry: '', grantItems: [], grantPage: 0,
          grantTotal: 0, hasMore: false, loadingMore: false, hasSession: hasMemberSession(),
          ledgerItems: [], ledgerPage: 0, ledgerTotal: 0,
          ledgerHasMore: false, ledgerLoadingMore: false,
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ loading: false });
    }
  },
  async onMoreTap() {
    if (!this.data.signedIn || !this.data.hasMore || this.data.loadingMore ||
        this.data.ledgerLoadingMore || this.data.loading) return;
    const current = viewGeneration;
    const page = this.data.grantPage + 1;
    this.setData({ loadingMore: true, error: '' });
    try {
      const grants = await getCurrentMemberPointsGrants(page);
      if (current !== viewGeneration) return;
      if (!grants) throw new Error('会员会话已失效，请重新登录');
      const items = this.data.grantItems.concat(
        grants.items.map((grant, index) => displayGrant(grant, index, page)));
      this.setData({
        grantItems: items, grantPage: page, grantTotal: grants.total,
        hasMore: items.length < grants.total,
      });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          signedIn: false, memberPublicId: '', availablePoints: '', reservedPoints: '',
          grantCount: 0, nearestExpiry: '', grantItems: [], grantPage: 0,
          grantTotal: 0, hasMore: false, hasSession: hasMemberSession(),
          ledgerItems: [], ledgerPage: 0, ledgerTotal: 0,
          ledgerHasMore: false, ledgerLoadingMore: false,
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ loadingMore: false });
    }
  },
  async onMoreLedgerTap() {
    if (!this.data.signedIn || !this.data.ledgerHasMore ||
        this.data.ledgerLoadingMore || this.data.loadingMore || this.data.loading) return;
    const current = viewGeneration;
    const page = this.data.ledgerPage + 1;
    this.setData({ ledgerLoadingMore: true, error: '' });
    try {
      const ledger = await getCurrentMemberPointsLedger(page);
      if (current !== viewGeneration) return;
      if (!ledger) throw new Error('会员会话已失效，请重新登录');
      const items = this.data.ledgerItems.concat(
        ledger.items.map((entry, index) => displayLedger(entry, index, page)));
      this.setData({
        ledgerItems: items, ledgerPage: page, ledgerTotal: ledger.total,
        ledgerHasMore: items.length < ledger.total,
      });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          signedIn: false, memberPublicId: '', availablePoints: '', reservedPoints: '',
          grantCount: 0, nearestExpiry: '', grantItems: [], grantPage: 0,
          grantTotal: 0, hasMore: false, loadingMore: false,
          ledgerItems: [], ledgerPage: 0, ledgerTotal: 0, ledgerHasMore: false,
          hasSession: hasMemberSession(),
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ ledgerLoadingMore: false });
    }
  },
  onRetryTap() { this.refreshPoints(); },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
  onCartTap() { wx.reLaunch({ url: '/pages/cart/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
