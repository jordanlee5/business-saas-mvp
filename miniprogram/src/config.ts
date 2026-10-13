// 本地地址只允许开发版的同机微信开发者工具模拟器使用。
const LOCAL_API_ORIGIN = 'http://127.0.0.1:8000';

// 取得正式 HTTPS 域名并在微信公众平台配置合法域名后，再于发布构建中填写。
// 不包含路径、尾斜杠或 AppSecret；留空时真机与发布环境明确停止请求。
const HTTPS_API_ORIGIN = '';

export class ApiOriginConfigurationError extends Error {}

export function resolveApiOrigin(envVersion: string, platform: string,
                                 httpsOrigin = HTTPS_API_ORIGIN): string {
  if (envVersion === 'develop' && platform === 'devtools') return LOCAL_API_ORIGIN;
  if (!/^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(httpsOrigin)) {
    throw new ApiOriginConfigurationError('当前环境尚未配置 HTTPS 商城服务地址');
  }
  return httpsOrigin;
}

export function getApiOrigin(): string {
  try {
    const account = wx.getAccountInfoSync();
    const device = wx.getSystemInfoSync();
    return resolveApiOrigin(account.miniProgram.envVersion, device.platform);
  } catch (error) {
    if (error instanceof ApiOriginConfigurationError) throw error;
    throw new ApiOriginConfigurationError('无法确认小程序运行环境');
  }
}
