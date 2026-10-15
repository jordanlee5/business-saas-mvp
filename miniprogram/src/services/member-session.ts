import { getApiOrigin } from '../config';

export interface MemberIdentity { member_public_id: string }
export interface MemberPointsSummary {
  available_points: string;
  reserved_points: string;
  grant_count: number;
  nearest_expires_at: string | null;
}

let token = '';
let expiresAt = 0;
let generation = 0;

export class MemberSessionError extends Error {
  constructor(message: string, readonly status = 0) { super(message); }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function currentToken(): string {
  if (token && Date.now() + 30000 < expiresAt) return token;
  clearMemberSession();
  return '';
}

export function clearMemberSession(): void {
  token = '';
  expiresAt = 0;
  generation += 1;
}

export function hasMemberSession(): boolean { return Boolean(currentToken()); }

function loginCode(): Promise<string> {
  return new Promise((resolve, reject) => wx.login({
    success(result) {
      if (typeof result.code === 'string' && result.code.length > 0 && result.code.length <= 512) {
        resolve(result.code);
      } else reject(new MemberSessionError('微信登录暂不可用，请稍后重试'));
    },
    fail() { reject(new MemberSessionError('微信登录暂不可用，请稍后重试')); },
  }));
}

function request(path: string, method: 'GET' | 'POST',
                 authToken = '', code = ''): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let origin: string;
    try { origin = getApiOrigin(); }
    catch (error) { reject(error); return; }
    wx.request({
      url: origin + path,
      method,
      timeout: 8000,
      header: {
        Accept: 'application/json',
        ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}),
        ...(authToken ? { Authorization: 'Bearer ' + authToken } : {}),
      },
      ...(method === 'POST' ? { data: { code } } : {}),
      success(response) {
        if (response.statusCode === 200) { resolve(response.data); return; }
        if (response.statusCode === 403) {
          reject(new MemberSessionError('此微信身份尚未绑定会员', 403)); return;
        }
        if (response.statusCode === 401) {
          reject(new MemberSessionError('会员会话已失效，请重新登录', 401)); return;
        }
        reject(new MemberSessionError('会员服务暂不可用，请稍后重试', response.statusCode));
      },
      fail() { reject(new MemberSessionError('网络连接失败，请稍后重试')); },
    });
  });
}

async function verifyMember(authToken: string): Promise<MemberIdentity> {
  const data = await request('/api/miniprogram/v1/auth/me', 'GET', authToken);
  if (!isRecord(data) || typeof data.member_public_id !== 'string' ||
      !/^[A-Za-z0-9_-]{1,80}$/.test(data.member_public_id)) {
    throw new MemberSessionError('会员身份数据暂不可用');
  }
  return { member_public_id: data.member_public_id };
}

export async function loginMember(): Promise<MemberIdentity> {
  clearMemberSession();
  const current = generation;
  // Only a user action calls this function. The one-use code is never persisted.
  const code = await loginCode();
  if (current !== generation) throw new MemberSessionError('本次登录已取消');
  const data = await request('/api/miniprogram/v1/auth/wechat-session', 'POST', '', code);
  if (current !== generation) throw new MemberSessionError('本次登录已取消');
  if (!isRecord(data) || data.token_type !== 'Bearer' ||
      typeof data.token !== 'string' || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(data.token) ||
      typeof data.expires_in !== 'number' || !Number.isInteger(data.expires_in) ||
      data.expires_in < 1 || data.expires_in > 900) {
    throw new MemberSessionError('会员会话数据暂不可用');
  }
  const deadline = Date.now() + data.expires_in * 1000;
  const identity = await verifyMember(data.token);
  if (current !== generation) throw new MemberSessionError('本次登录已取消');
  token = data.token;
  expiresAt = deadline;
  if (!currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
  return identity;
}

export async function getSignedInMember(): Promise<MemberIdentity | null> {
  const activeToken = currentToken();
  if (!activeToken) return null;
  const current = generation;
  try {
    const member = await verifyMember(activeToken);
    if (current !== generation || !currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
    return member;
  }
  catch (error) {
    if (current === generation && error instanceof MemberSessionError && error.status === 401) clearMemberSession();
    throw error;
  }
}

export async function getCurrentMemberPointsSummary(): Promise<MemberPointsSummary | null> {
  const activeToken = currentToken();
  if (!activeToken) return null;
  const current = generation;
  let data: unknown;
  try {
    data = await request('/api/miniprogram/v1/points/summary', 'GET', activeToken);
  } catch (error) {
    if (current === generation && error instanceof MemberSessionError && error.status === 401) clearMemberSession();
    throw error;
  }
  if (current !== generation || !currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
  const points = (value: unknown): value is string =>
    typeof value === 'string' && /^\d+\.\d{2}$/.test(value);
  if (!isRecord(data) || !points(data.available_points) || !points(data.reserved_points) ||
      typeof data.grant_count !== 'number' || !Number.isSafeInteger(data.grant_count) ||
      data.grant_count < 0 ||
      !(data.nearest_expires_at === null ||
        (typeof data.nearest_expires_at === 'string' &&
         /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/.test(data.nearest_expires_at) &&
         Number.isFinite(Date.parse(data.nearest_expires_at))))) {
    throw new MemberSessionError('积分数据暂不可用');
  }
  return {
    available_points: data.available_points,
    reserved_points: data.reserved_points,
    grant_count: data.grant_count,
    nearest_expires_at: data.nearest_expires_at as string | null,
  };
}
