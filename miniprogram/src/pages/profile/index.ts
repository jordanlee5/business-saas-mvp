import { clearMemberSession, getSignedInMember, hasMemberSession, loginMember } from '../../services/member-session';

let viewGeneration = 0;

Page({
  data: {
    loading: false,
    signingIn: false,
    signedIn: false,
    memberPublicId: '',
    error: '',
  },
  onShow() { this.refreshIdentity(); },
  onHide() { viewGeneration += 1; },
  onUnload() { viewGeneration += 1; },
  async refreshIdentity() {
    const current = ++viewGeneration;
    const hasSession = hasMemberSession();
    this.setData({ signedIn: false, memberPublicId: '', error: '', loading: hasSession });
    if (!hasSession) return;
    try {
      const member = await getSignedInMember();
      if (current === viewGeneration && member) {
        this.setData({ signedIn: true, memberPublicId: member.member_public_id });
      }
    } catch (error) {
      if (current === viewGeneration) this.setData({ error: error instanceof Error ? error.message : '会员服务暂不可用' });
    } finally {
      if (current === viewGeneration) this.setData({ loading: false });
    }
  },
  async onLoginTap() {
    if (this.data.signingIn || this.data.loading) return;
    const current = ++viewGeneration;
    this.setData({ signingIn: true, signedIn: false, memberPublicId: '', error: '' });
    try {
      const member = await loginMember();
      if (current === viewGeneration) {
        this.setData({ signedIn: true, memberPublicId: member.member_public_id });
      }
    } catch (error) {
      if (current === viewGeneration) this.setData({ error: error instanceof Error ? error.message : '会员登录暂不可用' });
    } finally {
      if (current === viewGeneration) this.setData({ signingIn: false });
    }
  },
  onClearSessionTap() {
    clearMemberSession();
    viewGeneration += 1;
    this.setData({ signedIn: false, memberPublicId: '', error: '', signingIn: false, loading: false });
  },
  onPointsTap() { wx.reLaunch({ url: '/pages/points/index' }); },
  onActivationTap() { wx.navigateTo({ url: '/pages/activation/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
