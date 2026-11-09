import { hasMemberSession, requestMemberPrivate, MemberSessionError } from './member-session';
import { allowCatalog, invalidateCatalog, catalogEpoch, type AccessState } from './catalog-state';

export const accessMessages: Record<AccessState, string> = {
  checking: '正在核验商城资格…',
  login: '商城面向已确认名单内的会员。请先微信登录；首次使用可凭服务方交付的资料激活资格。',
  eligible: '商城资格有效，可以浏览商品和兑换。',
  disabled: '商城资格已停用，请联系服务方核对。本人历史订单、物流和权益申请仍可查询。',
  expired: '商城资格已到期，请联系服务方核对有效期。本人历史订单、物流和权益申请仍可查询。',
  pending: '商城资格尚未到生效时间，请联系服务方核对。',
  unconfirmed: '暂无有效商城资格，请联系服务方确认名单与绑定；本人历史服务入口仍保留。',
  unavailable: '暂时无法核验商城资格，请稍后重试。本人历史服务入口仍保留。',
};

export async function checkCatalogAccess(): Promise<AccessState> {
  if (!hasMemberSession()) { invalidateCatalog('login'); return 'login'; }
  const epoch = catalogEpoch(), started = Date.now();
  try {
    const result = await requestMemberPrivate('/catalog-access', 'GET') as {status?: AccessState; valid_for_seconds?: number};
    if (epoch !== catalogEpoch()) throw new MemberSessionError('资格核验已取消');
    if (!result || !['eligible','disabled','expired','pending','unconfirmed'].includes(result.status || '') ||
        typeof result.valid_for_seconds !== 'number' || !Number.isFinite(result.valid_for_seconds) ||
        result.valid_for_seconds < 0 || result.valid_for_seconds > 900) throw new Error('资格数据异常');
    if (result.status === 'eligible') {
      // Use request start, so a slow response never extends the server's expiry.
      const until = started + result.valid_for_seconds * 1000;
      if (until <= Date.now()) { invalidateCatalog('expired'); return 'expired'; }
      allowCatalog(until);
    } else invalidateCatalog(result.status!);
    return result.status!;
  } catch (error) {
    if (epoch !== catalogEpoch()) throw error;
    invalidateCatalog('unavailable', false);
    return 'unavailable';
  }
}
