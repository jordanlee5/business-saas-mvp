import { requestMemberPrivate, MemberSessionError } from './member-session';
import { normalizeAddress, type AddressInput } from './addresses';
import type { CartDisplayLine } from './cart';
import {parseFulfillment,type FulfillmentNotice} from './fulfillment';
export interface OrderSummary { order_public_id: string; status: string; total_points: string; total_quantity: number; created_at: string }
export interface OrderDetail extends OrderSummary { address: AddressInput | null; address_state:'AVAILABLE'|'CLEARED'|'NOT_RECORDED'; fulfillment:FulfillmentNotice|null; items: {product_name:string;sku_name:string;quantity:number;unit_points_price:string;line_points:string}[]; shipping_carrier:string|null;tracking_number:string|null }
export const statusLabels: Record<string,string> = {CREATED:'待处理',CANCELLED:'已取消',FULFILLING:'待发货',SHIPPED:'已发货',COMPLETED:'已完成',REFUNDED:'已退款'};
const pendingKey='mall_checkout_pending_v1';
let memory: {owner:string;body:Record<string,unknown>} | null=null;
function summary(data: unknown): OrderSummary {
  const x=data as OrderSummary;
  if (!x || !/^ORD-[A-Z0-9]{16}$/.test(x.order_public_id) || !statusLabels[x.status] ||
      !/^\d+\.\d{2}$/.test(x.total_points) || !Number.isSafeInteger(x.total_quantity) || x.total_quantity<1 ||
      typeof x.created_at!=='string' || !Number.isFinite(Date.parse(x.created_at))) throw new MemberSessionError('订单数据暂不可用');
  return {order_public_id:x.order_public_id,status:x.status,total_points:x.total_points,total_quantity:x.total_quantity,created_at:x.created_at};
}
function detail(data:unknown):OrderDetail {
  const x=data as OrderDetail; const base=summary(data);
  if (!Array.isArray(x.items) || !x.items.length || x.items.some(i=>!i || typeof i.product_name!=='string' || typeof i.sku_name!=='string' || !Number.isSafeInteger(i.quantity) || i.quantity<1 || !/^\d+\.\d{2}$/.test(i.unit_points_price) || !/^\d+\.\d{2}$/.test(i.line_points)) ||
      !(x.shipping_carrier===null || typeof x.shipping_carrier==='string') || !(x.tracking_number===null || typeof x.tracking_number==='string')) throw new MemberSessionError('订单详情暂不可用');
  try {
    const address=x.address===null?null:normalizeAddress(x.address);
    const state=x.address_state || (address?'AVAILABLE':'NOT_RECORDED');
    if(!['AVAILABLE','CLEARED','NOT_RECORDED'].includes(state)||(state==='AVAILABLE')!==Boolean(address))throw Error();
    return {...base,address,address_state:state,fulfillment:x.fulfillment?parseFulfillment(x.fulfillment):null,items:x.items,shipping_carrier:x.shipping_carrier,tracking_number:x.tracking_number}; }
  catch (_) { throw new MemberSessionError('订单详情暂不可用，请查询提交结果'); }
}
export async function listOrders(page:number) {
  const x=await requestMemberPrivate('/orders?page='+page,'GET') as {items:unknown[];total:number;page:number;page_size:number};
  if (!x || !Array.isArray(x.items) || x.items.length>20 || x.page!==page || x.page_size!==20 || !Number.isSafeInteger(x.total) || x.total<0) throw new MemberSessionError('订单列表暂不可用');
  return {...x,items:x.items.map(summary)};
}
export async function getOrder(id:string) { return detail(await requestMemberPrivate('/orders/'+id,'GET')); }
export async function cancelOrder(id:string) { return detail(await requestMemberPrivate('/orders/'+id+'/cancel','POST')); }
export function pendingCheckout(owner:string):string {
  const x=wx.getStorageSync(pendingKey+':'+owner) as {owner?:string;request_id?:string}|null;
  if (x && x.owner===owner) {
    if (typeof x.request_id!=='string' || !/^[a-f0-9]{32}$/.test(x.request_id)) throw new MemberSessionError('待确认订单记录异常，请先核对我的订单');
    return x.request_id;
  }
  return '';
}
export function beginCheckout(owner:string,addressId:string,address:AddressInput,items:CartDisplayLine[],fulfillmentDigest=''):void {
  // A correlation key, never an authentication credential. It is scoped to the member on the server.
  const request_id=pendingCheckout(owner) || (Date.now().toString(16)+Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('')).slice(0,32);
  const body={request_id,address_public_id:addressId,expected_address:normalizeAddress(address),...(fulfillmentDigest?{fulfillment_digest:fulfillmentDigest}:{}),lines:items.map(x=>({product_public_id:x.product_public_id,sku_code:x.sku_code,quantity:x.quantity,expected_points_price:x.points_price}))};
  wx.setStorageSync(pendingKey+':'+owner,{owner,request_id,lines:items.map(x=>({product_public_id:x.product_public_id,sku_code:x.sku_code,quantity:x.quantity}))}); // No recipient, address, token or order body on disk.
  memory={owner,body};
}
export function canRetryCheckout(owner:string):boolean { return Boolean(memory && memory.owner===owner && memory.body.request_id===pendingCheckout(owner)); }
export function clearPendingCheckout(owner:string):void {
  if (pendingCheckout(owner)) wx.setStorageSync(pendingKey+':'+owner,null);
  if(memory && memory.owner===owner) memory=null;
}
export async function submitCheckout(owner:string):Promise<OrderDetail> {
  if (!canRetryCheckout(owner)) throw new MemberSessionError('请查询待确认订单');
  const data=await requestMemberPrivate('/orders','POST',memory!.body);
  return detail(data);
}
export async function resolveCheckout(owner:string):Promise<OrderDetail> {
  const id=pendingCheckout(owner);if(!id)throw new MemberSessionError('没有待确认订单');
  return detail(await requestMemberPrivate('/orders/request/'+id,'GET'));
}

export function pendingCartLines(owner:string): {product_public_id:string;sku_code:string;quantity:number}[] {
  if(!pendingCheckout(owner))return [];
  const value=wx.getStorageSync(pendingKey+':'+owner) as {lines?:{product_public_id:string;sku_code:string;quantity:number}[]};
  return Array.isArray(value.lines)?value.lines:[];
}
