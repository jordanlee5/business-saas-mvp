import { redeemActivation } from '../../services/activation';

let viewGeneration = 0;

Page({
  data: {
    businessPublicNo: '', phone: '', plateNumber: '', activationCode: '',
    submitting: false, activated: false, error: '',
  },
  onHide() { this.clearPrivateInput(); },
  onUnload() { this.clearPrivateInput(); },
  clearPrivateInput() {
    viewGeneration += 1;
    this.setData({ businessPublicNo: '', phone: '', plateNumber: '', activationCode: '', submitting: false });
  },
  onBusinessInput(event) { this.setData({ businessPublicNo: event.detail.value }); },
  onPhoneInput(event) { this.setData({ phone: event.detail.value }); },
  onPlateInput(event) { this.setData({ plateNumber: event.detail.value }); },
  onCodeInput(event) { this.setData({ activationCode: event.detail.value }); },
  async onSubmitTap() {
    if (this.data.submitting || this.data.activated) return;
    const current = ++viewGeneration;
    this.setData({ submitting: true, error: '' });
    try {
      await redeemActivation({
        businessPublicNo: this.data.businessPublicNo,
        phone: this.data.phone,
        plateNumber: this.data.plateNumber,
        activationCode: this.data.activationCode,
      });
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
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
});
