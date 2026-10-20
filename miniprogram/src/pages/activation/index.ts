import { redeemActivation, validateActivationInput } from '../../services/activation';
import { clearMemberSession, loginMember } from '../../services/member-session';
import { getPrivacySetting, openPrivacyContract } from '../../services/privacy';
import { getApiOrigin } from '../../config';

let viewGeneration = 0;

Page({
  data: {
    businessPublicNo: '', phone: '', plateNumber: '', activationCode: '',
    submitting: false, signingIn: false, activated: false, error: '',
    privacyVisible: false, privacyContractName: '', privacyAction: '', privacyError: '',
  },
  onHide() { this.leavePage(); },
  onUnload() { this.leavePage(); },
  leavePage() {
    if (this.data.signingIn) clearMemberSession();
    this.clearPrivateInput();
    this.setData({ signingIn: false, privacyVisible: false, privacyAction: '', privacyError: '' });
  },
  clearPrivateInput() {
    viewGeneration += 1;
    this.setData({ businessPublicNo: '', phone: '', plateNumber: '', activationCode: '', submitting: false });
  },
  onBusinessInput(event) { this.setData({ businessPublicNo: event.detail.value }); },
  onPhoneInput(event) { this.setData({ phone: event.detail.value }); },
  onPlateInput(event) { this.setData({ plateNumber: event.detail.value }); },
  onCodeInput(event) { this.setData({ activationCode: event.detail.value }); },
  async onSubmitTap() {
    if (this.data.submitting || this.data.activated || this.data.privacyVisible) return;
    const current = ++viewGeneration;
    this.setData({ submitting: true, error: '' });
    try {
      const input = {
        businessPublicNo: this.data.businessPublicNo,
        phone: this.data.phone,
        plateNumber: this.data.plateNumber,
        activationCode: this.data.activationCode,
      };
      validateActivationInput(input);
      getApiOrigin();
      const privacy = await getPrivacySetting();
      if (current !== viewGeneration) return;
      if (privacy.needAuthorization) {
        this.setData({ privacyVisible: true, privacyContractName: privacy.contractName,
          privacyAction: 'activate', privacyError: '' });
        return;
      }
      await redeemActivation(input);
      if (current === viewGeneration) {
        this.setData({ activated: true, businessPublicNo: '', phone: '', plateNumber: '', activationCode: '' });
      }
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          activationCode: '',
          error: error instanceof Error ? error.message : '权益激活暂不可用，请稍后重试',
        });
      }
    } finally {
      if (current === viewGeneration) this.setData({ submitting: false });
    }
  },
  async onLoginAndPointsTap() {
    if (!this.data.activated || this.data.signingIn || this.data.privacyVisible) return;
    const current = ++viewGeneration;
    this.setData({ signingIn: true, error: '' });
    try {
      const privacy = await getPrivacySetting();
      if (current !== viewGeneration) return;
      if (privacy.needAuthorization) {
        this.setData({ signingIn: false, privacyVisible: true, privacyContractName: privacy.contractName,
          privacyAction: 'login', privacyError: '' });
        return;
      }
      // loginMember obtains a new wx.login code; the activation code is never reused.
      await loginMember();
      if (current !== viewGeneration) return;
      this.setData({ signingIn: false });
      wx.reLaunch({ url: '/pages/points/index' });
    } catch (error) {
      if (current === viewGeneration) {
        this.setData({
          signingIn: false,
          error: error instanceof Error ? error.message : '会员登录暂不可用，请稍后重试',
        });
      }
    }
  },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
  onPrivacyAgree() {
    if (!this.data.privacyVisible) return;
    const action = this.data.privacyAction;
    this.setData({ privacyVisible: false, privacyAction: '', privacyError: '' });
    if (action === 'activate') this.onSubmitTap();
    else if (action === 'login') this.onLoginAndPointsTap();
  },
  onPrivacyDecline() {
    this.setData({ privacyVisible: false, privacyAction: '', privacyError: '' });
  },
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
});
