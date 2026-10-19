import { getApiOrigin } from '../config';

export interface ActivationInput {
  businessPublicNo: string;
  phone: string;
  plateNumber: string;
  activationCode: string;
}

export class ActivationError extends Error {}

export function validateActivationInput(input: ActivationInput): ActivationInput {
  const businessPublicNo = input.businessPublicNo.trim();
  const phone = input.phone.replace(/\s/g, '');
  const plateNumber = input.plateNumber.trim().toUpperCase();
  const activationCode = input.activationCode.replace(/[\s-]/g, '').toUpperCase();
  if (!businessPublicNo || businessPublicNo.length > 80 ||
      !/^\d{1,32}$/.test(phone) ||
      !plateNumber || plateNumber.length > 32 ||
      !/^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{10}$/.test(activationCode)) {
    throw new ActivationError('请检查业务单号、手机号、车牌号和十位激活码');
  }
  return { businessPublicNo, phone, plateNumber, activationCode };
}

function freshWechatCode(): Promise<string> {
  return new Promise((resolve, reject) => wx.login({
    success(result) {
      if (typeof result.code === 'string' && result.code.length > 0 && result.code.length <= 512) {
        resolve(result.code);
      } else reject(new ActivationError('微信身份验证暂不可用，请稍后重试'));
    },
    fail() { reject(new ActivationError('微信身份验证暂不可用，请稍后重试')); },
  }));
}

export async function redeemActivation(input: ActivationInput): Promise<void> {
  const values = validateActivationInput(input);
  // Resolve the environment before consuming the one-use WeChat code.
  const origin = getApiOrigin();
  const wechatCode = await freshWechatCode();
  return new Promise<void>((resolve, reject) => wx.request({
    url: origin + '/api/miniprogram/v1/activation/redeem',
    method: 'POST', timeout: 8000,
    header: { Accept: 'application/json', 'Content-Type': 'application/json' },
    data: {
      wechat_code: wechatCode,
      business_public_no: values.businessPublicNo,
      phone: values.phone,
      plate_number: values.plateNumber,
      activation_code: values.activationCode,
    },
    success(response) {
      if (response.statusCode === 200 && response.data !== null &&
          typeof response.data === 'object' && !Array.isArray(response.data) &&
          (response.data as { status?: unknown }).status === 'activated') {
        resolve(); return;
      }
      if (response.statusCode === 400) {
        reject(new ActivationError('激活信息无效或权益当前不可领取')); return;
      }
      if (response.statusCode === 401) {
        reject(new ActivationError('微信身份验证已失效，请重新提交')); return;
      }
      if (response.statusCode === 422) {
        reject(new ActivationError('输入信息格式有误，请核对后重试')); return;
      }
      reject(new ActivationError('权益激活暂不可用，请稍后重试'));
    },
    fail() { reject(new ActivationError('网络连接失败，请确认结果后再试')); },
  }));
}
