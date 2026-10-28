import { MemberSessionError, requestMemberAddress } from './member-session';

export type AddressInput = { recipient_name: string; phone: string; province: string; city: string; district: string; detail: string };
export type Address = AddressInput & { address_public_id: string; is_default: boolean };
export const fields = ['recipient_name', 'phone', 'province', 'city', 'district', 'detail'] as const;
const labels = ['收件人', '电话', '省', '市', '区县', '详细地址'];
export function blankAddress(): AddressInput { return { recipient_name: '', phone: '', province: '', city: '', district: '', detail: '' }; }
export function normalizeAddress(input: AddressInput): AddressInput {
  const result = blankAddress();
  fields.forEach((key, index) => {
    const raw = typeof input[key] === 'string' ? input[key] : '';
    const value = raw.trim();
    const limit = key === 'detail' ? 240 : key === 'phone' ? 16 : 80;
    if (!value || Array.from(raw).length > limit || /\p{C}/u.test(raw)) {
      throw new MemberSessionError('请填写有效的' + labels[index] + '（最多' + limit + '字）', 422);
    }
    result[key] = value;
  });
  if (!/^\+?[0-9]{7,15}$/.test(result.phone)) throw new MemberSessionError('电话须为7–15位数字，可带前导+号', 422);
  return result;
}
function parseAddress(value: unknown): Address {
  const item = value as Address;
  if (!item || typeof item !== 'object' || typeof item.address_public_id !== 'string' ||
      !/^ADDR-[0-9a-f]{32}$/.test(item.address_public_id) || typeof item.is_default !== 'boolean') {
    throw new MemberSessionError('地址数据暂不可用，请刷新后核对结果');
  }
  try { return { ...normalizeAddress(item), address_public_id: item.address_public_id, is_default: item.is_default }; }
  catch (_) { throw new MemberSessionError('地址数据暂不可用，请刷新后核对结果'); }
}
export async function listAddresses(): Promise<Address[]> {
  const data = await requestMemberAddress('/addresses', 'GET') as { items: unknown[]; total: number; limit: number };
  if (!data || !Array.isArray(data.items) || data.limit !== 20 || data.total !== data.items.length || data.total > 20) {
    throw new MemberSessionError('地址列表暂不可用，请重试');
  }
  const items = data.items.map(parseAddress);
  if (new Set(items.map(item => item.address_public_id)).size !== items.length || items.filter(item => item.is_default).length > 1) {
    throw new MemberSessionError('地址列表暂不可用，请重试');
  }
  return items;
}
export async function saveAddress(id: string, input: AddressInput): Promise<void> {
  parseAddress(await requestMemberAddress(id ? '/addresses/' + id : '/addresses', id ? 'PUT' : 'POST', normalizeAddress(input)));
}
export async function removeAddress(id: string): Promise<void> { await requestMemberAddress('/addresses/' + id, 'DELETE'); }
export async function defaultAddress(id: string): Promise<void> { parseAddress(await requestMemberAddress('/addresses/' + id + '/default', 'PUT')); }
