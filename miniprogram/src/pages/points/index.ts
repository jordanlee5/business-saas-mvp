import {
  getCurrentMemberPointsGrants, getCurrentMemberPointsSummary,
  getSignedInMember, hasMemberSession, type MemberPointsGrant,
} from '../../services/member-session';

let viewGeneration = 0;
type GrantDisplay = {
  key: string;
  granted: string; available: string; reserved: string;
  activated: string; expires: string; status: string;
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
      this.setData({
        signedIn: true, memberPublicId: member.member_public_id,
        availablePoints: summary.available_points, reservedPoints: summary.reserved_points,
        grantCount: summary.grant_count,
        nearestExpiry: summary.nearest_expires_at
          ? dateText(summary.nearest_expires_at) : '',
        grantItems: grants.items.map((grant, index) => displayGrant(grant, index, 1)),
        grantPage: 1, grantTotal: grants.total,
        hasMore: grants.items.length < grants.total,
      });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          signedIn: false, memberPublicId: '', availablePoints: '', reservedPoints: '',
          grantCount: 0, nearestExpiry: '', grantItems: [], grantPage: 0,
          grantTotal: 0, hasMore: false, loadingMore: false, hasSession: hasMemberSession(),
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ loading: false });
    }
  },
  async onMoreTap() {
    if (!this.data.signedIn || !this.data.hasMore || this.data.loadingMore || this.data.loading) return;
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
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ loadingMore: false });
    }
  },
  onRetryTap() { this.refreshPoints(); },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
