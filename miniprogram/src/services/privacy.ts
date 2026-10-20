export class PrivacyAuthorizationError extends Error {}

const unavailable = '隐私保护指引尚未就绪，请稍后再试';

// Configure the exact production AppID only in a later platform-configuration
// release. A formal-looking value returned by DevTools is not proof that this
// project has an approved privacy guide. Empty here deliberately fails closed.
const APPROVED_PRIVACY_APP_ID = '';

export interface PrivacySetting { needAuthorization: boolean; contractName: string }

export function getPrivacySetting(): Promise<PrivacySetting> {
  return new Promise((resolve, reject) => {
    try {
      const appId = wx.getAccountInfoSync().miniProgram.appId;
      if (!/^wx[0-9a-f]{16}$/i.test(APPROVED_PRIVACY_APP_ID) ||
          appId !== APPROVED_PRIVACY_APP_ID) {
        reject(new PrivacyAuthorizationError(unavailable));
        return;
      }
    } catch (_) {
      reject(new PrivacyAuthorizationError(unavailable));
      return;
    }
    if (typeof wx.getPrivacySetting !== 'function') {
      reject(new PrivacyAuthorizationError(unavailable));
      return;
    }
    try {
      wx.getPrivacySetting({
        success(result) {
          const name = result && typeof result.privacyContractName === 'string'
            ? result.privacyContractName.trim() : '';
          if (name && typeof result.needAuthorization === 'boolean') {
            resolve({ needAuthorization: result.needAuthorization, contractName: name });
          } else reject(new PrivacyAuthorizationError(unavailable));
        },
        fail() { reject(new PrivacyAuthorizationError(unavailable)); },
      });
    } catch (_) { reject(new PrivacyAuthorizationError(unavailable)); }
  });
}

// Page buttons use open-type="agreePrivacyAuthorization". Recheck WeChat's
// state here so direct service calls cannot bypass the platform decision.
export async function assertPrivacyAuthorized(): Promise<void> {
  if ((await getPrivacySetting()).needAuthorization) {
    throw new PrivacyAuthorizationError('请先同意微信隐私保护指引，再使用会员服务');
  }
}

export async function openPrivacyContract(): Promise<void> {
  await getPrivacySetting();
  if (typeof wx.openPrivacyContract !== 'function') {
    throw new PrivacyAuthorizationError(unavailable);
  }
  return new Promise((resolve, reject) => {
    try {
      wx.openPrivacyContract({
        success() { resolve(); },
        fail() { reject(new PrivacyAuthorizationError(unavailable)); },
      });
    } catch (_) { reject(new PrivacyAuthorizationError(unavailable)); }
  });
}
