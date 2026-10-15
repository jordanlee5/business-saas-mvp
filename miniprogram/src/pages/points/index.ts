import {
  getCurrentMemberPointsSummary, getSignedInMember, hasMemberSession,
} from '../../services/member-session';

let viewGeneration = 0;

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
      availablePoints: '', reservedPoints: '', grantCount: 0, nearestExpiry: '', error: '',
    });
    if (!hasSession) return;
    try {
      const member = await getSignedInMember();
      if (current !== viewGeneration) return;
      if (!member) throw new Error('会员会话已失效，请重新登录');
      const summary = await getCurrentMemberPointsSummary();
      if (current !== viewGeneration) return;
      if (!summary) throw new Error('会员会话已失效，请重新登录');
      this.setData({
        signedIn: true, memberPublicId: member.member_public_id,
        availablePoints: summary.available_points, reservedPoints: summary.reserved_points,
        grantCount: summary.grant_count,
        nearestExpiry: summary.nearest_expires_at
          ? summary.nearest_expires_at.replace('T', ' ').replace('+08:00', '（北京时间）') : '',
      });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          hasSession: hasMemberSession(),
          error: error instanceof Error ? error.message : '积分数据暂不可用',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ loading: false });
    }
  },
  onRetryTap() { this.refreshPoints(); },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
