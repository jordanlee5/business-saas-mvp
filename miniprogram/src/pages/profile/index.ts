import { clearMemberSession, getSignedInMember, hasMemberSession, loginMember } from '../../services/member-session';
import { getPrivacySetting, openPrivacyContract } from '../../services/privacy';

let viewGeneration = 0;

Page({
  data: {
    loading: false,
    signingIn: false,
    signedIn: false,
    memberPublicId: '',
    error: '',
    privacyVisible: false,
    privacyContractName: '',
    privacyError: '',
  },
  onShow() { this.refreshIdentity(); },
  onHide() { viewGeneration += 1; this.setData({ privacyVisible: false, privacyError: '' }); },
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
    if (this.data.signingIn || this.data.loading || this.data.privacyVisible) return;
    const current = ++viewGeneration;
    this.setData({ signingIn: true, signedIn: false, memberPublicId: '', error: '' });
    try {
      const privacy = await getPrivacySetting();
      if (current !== viewGeneration) return;
      if (privacy.needAuthorization) {
        this.setData({ privacyVisible: true, privacyContractName: privacy.contractName, privacyError: '' });
        return;
      }
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
  onAddressesTap() { wx.navigateTo({ url: '/pages/addresses/index' }); },
  onPointsTap() { wx.reLaunch({ url: '/pages/points/index' }); },
  onActivationTap() { wx.navigateTo({ url: '/pages/activation/index' }); },
  onPrivacyAgree() {
    if (!this.data.privacyVisible) return;
    this.setData({ privacyVisible: false, privacyError: '' });
    this.onLoginTap();
  },
  onPrivacyDecline() { this.setData({ privacyVisible: false, privacyError: '' }); },
  async onPrivacyTap() {
    const current = viewGeneration;
    try { await openPrivacyContract(); }
    catch (error) {
      if (current === viewGeneration) {
        const message = error instanceof Error ? error.message : '隐私保护指引暂不可用';
        this.setData(this.data.privacyVisible ? { privacyError: message } : { error: message });
      }
    }
  },
  onCartTap() { wx.reLaunch({ url: '/pages/cart/index' }); },
  onCatalogTap() { wx.reLaunch({ url: '/pages/catalog/index' }); },
});
