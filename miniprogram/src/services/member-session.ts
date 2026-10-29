import { getApiOrigin } from '../config';
import { assertPrivacyAuthorized } from './privacy';

export interface MemberIdentity { member_public_id: string }
export interface MemberPointsSummary {
  available_points: string;
  reserved_points: string;
  grant_count: number;
  nearest_expires_at: string | null;
}
export interface MemberPointsGrant {
  granted_points: string;
  available_points: string;
  reserved_points: string;
  activated_at: string;
  expires_at: string;
  status: 'ACTIVE' | 'EXHAUSTED' | 'EXPIRED' | 'FROZEN';
}
export interface MemberPointsGrantPage {
  items: MemberPointsGrant[];
  page: number;
  page_size: number;
  total: number;
}
export interface MemberPointsLedgerEntry {
  entry_type: 'GRANT' | 'RESERVE' | 'RELEASE' | 'CONSUME' | 'REFUND' | 'EXPIRE' | 'ADJUST';
  available_points_delta: string;
  reserved_points_delta: string;
  created_at: string;
}
export interface MemberPointsLedgerPage {
  items: MemberPointsLedgerEntry[];
  page: number;
  page_size: number;
  total: number;
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

function isPoints(value: unknown): value is string {
  return typeof value === 'string' && /^\d+\.\d{2}$/.test(value);
}

function isPointsDelta(value: unknown): value is string {
  return typeof value === 'string' && /^-?\d+\.\d{2}$/.test(value);
}

function isUtc8Timestamp(value: unknown): value is string {
  return typeof value === 'string' &&
    /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d\+08:00$/.test(value) &&
    Number.isFinite(Date.parse(value));
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

async function loginCode(): Promise<string> {
  await assertPrivacyAuthorized();
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
  if (!isRecord(data) || !isPoints(data.available_points) || !isPoints(data.reserved_points) ||
      typeof data.grant_count !== 'number' || !Number.isSafeInteger(data.grant_count) ||
      data.grant_count < 0 ||
      !(data.nearest_expires_at === null || isUtc8Timestamp(data.nearest_expires_at))) {
    throw new MemberSessionError('积分数据暂不可用');
  }
  return {
    available_points: data.available_points,
    reserved_points: data.reserved_points,
    grant_count: data.grant_count,
    nearest_expires_at: data.nearest_expires_at as string | null,
  };
}


export async function getCurrentMemberPointsGrants(page: number): Promise<MemberPointsGrantPage | null> {
  if (!Number.isSafeInteger(page) || page < 1 || page > 1000000) {
    throw new MemberSessionError('积分批次页码无效');
  }
  const activeToken = currentToken();
  if (!activeToken) return null;
  const current = generation;
  let data: unknown;
  try {
    data = await request(`/api/miniprogram/v1/points/grants?page=${page}&page_size=20`, 'GET', activeToken);
  } catch (error) {
    if (current === generation && error instanceof MemberSessionError && error.status === 401) clearMemberSession();
    throw error;
  }
  if (current !== generation || !currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
  if (!isRecord(data) || !Array.isArray(data.items) || data.items.length > 20 ||
      data.page !== page || data.page_size !== 20 ||
      !Number.isSafeInteger(data.total) || (data.total as number) < 0 ||
      (data.total as number) < (page - 1) * 20 + data.items.length ||
      ((data.total as number) > (page - 1) * 20 && data.items.length === 0)) {
    throw new MemberSessionError('积分批次数据暂不可用');
  }
  const valid = data.items.every((item: unknown) => isRecord(item) &&
    isPoints(item.granted_points) && isPoints(item.available_points) &&
    isPoints(item.reserved_points) && isUtc8Timestamp(item.activated_at) &&
    isUtc8Timestamp(item.expires_at) &&
    ['ACTIVE', 'EXHAUSTED', 'EXPIRED', 'FROZEN'].includes(String(item.status)));
  if (!valid) throw new MemberSessionError('积分批次数据暂不可用');
  return data as unknown as MemberPointsGrantPage;
}

export async function getCurrentMemberPointsLedger(page: number, availableOnly = false): Promise<MemberPointsLedgerPage | null> {
  if (!Number.isSafeInteger(page) || page < 1 || page > 1000000) {
    throw new MemberSessionError('积分流水页码无效');
  }
  const activeToken = currentToken();
  if (!activeToken) return null;
  const current = generation;
  let data: unknown;
  try {
    data = await request(`/api/miniprogram/v1/points/ledger?page=${page}&page_size=20${availableOnly ? "&available_only=true" : ""}`, 'GET', activeToken);
  } catch (error) {
    if (current === generation && error instanceof MemberSessionError && error.status === 401) clearMemberSession();
    throw error;
  }
  if (current !== generation || !currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
  if (!isRecord(data) || !Array.isArray(data.items) || data.items.length > 20 ||
      data.page !== page || data.page_size !== 20 ||
      !Number.isSafeInteger(data.total) || (data.total as number) < 0 ||
      (data.total as number) < (page - 1) * 20 + data.items.length ||
      ((data.total as number) > (page - 1) * 20 && data.items.length === 0)) {
    throw new MemberSessionError('积分流水数据暂不可用');
  }
  const valid = data.items.every((item: unknown) => isRecord(item) &&
    ['GRANT', 'RESERVE', 'RELEASE', 'CONSUME', 'REFUND', 'EXPIRE', 'ADJUST'].includes(String(item.entry_type)) &&
    isPointsDelta(item.available_points_delta) && isPointsDelta(item.reserved_points_delta) &&
    (!availableOnly || !/^-?0+\.00$/.test(item.available_points_delta)) &&
    isUtc8Timestamp(item.created_at));
  if (!valid) throw new MemberSessionError('积分流水数据暂不可用');
  return data as unknown as MemberPointsLedgerPage;
}

// Address transport keeps the bearer private and rejects responses from old sessions.
export async function requestMemberPrivate(path: string, method: 'GET' | 'POST' | 'PUT' | 'DELETE',
                                           data?: Record<string, unknown>): Promise<unknown> {
  if (!/^\/addresses(?:\/ADDR-[0-9a-f]{32}(?:\/default)?)?$/.test(path) &&
      !/^\/orders(?:\?page=[1-9][0-9]{0,5}|\/fulfillment-preview|\/request\/[a-f0-9]{32}|\/ORD-[A-Z0-9]{16}(?:\/cancel)?)?$/.test(path)) {
    throw new MemberSessionError('收货地址请求无效');
  }
  const activeToken = currentToken();
  if (!activeToken) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
  const current = generation;
  try {
    const result = await new Promise<unknown>((resolve, reject) => wx.request({
      url: getApiOrigin() + '/api/miniprogram/v1' + path, method, timeout: 8000,
      header: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: 'Bearer ' + activeToken },
      ...(data ? { data } : {}),
      success(response) {
        const expected = method === 'POST' ? 201 : method === 'DELETE' ? 204 : 200;
        if (response.statusCode === expected || (path.startsWith('/orders') && response.statusCode === 200)) { resolve(response.data); return; }
        const messages: Record<number, string> = {
          401: '会员会话已失效，请重新登录', 404: '地址已不存在，请刷新列表',
          409: '最多保存20条地址，请先删除不再使用的地址', 422: '地址格式不正确，请检查后重试',
        };
        const orderMessages: Record<number, string> = {401: '会员会话已失效，请重新登录', 404: '尚未查询到订单或地址，请核对后重试', 409: '商品、价格、地址、积分、库存或订单状态已变化，请重新核对', 422: '订单请求格式不正确'};
        reject(new MemberSessionError((path.startsWith('/orders') ? orderMessages : messages)[response.statusCode] || '服务暂不可用，请查询确认操作结果', response.statusCode));
      },
      fail() { reject(new MemberSessionError('网络连接失败，请刷新后核对操作结果')); },
    }));
    if (current !== generation || !currentToken()) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
    return result;
  } catch (error) {
    if (current !== generation) throw new MemberSessionError('会员会话已失效，请重新登录', 401);
    if (error instanceof MemberSessionError && error.status === 401) clearMemberSession();
    throw error;
  }
}

export const requestMemberAddress = requestMemberPrivate;
