import {requestMemberPrivate, MemberSessionError} from './member-session';
import type {CartDisplayLine} from './cart';
export interface FulfillmentNotice {
  ready:boolean; message:string; notice:string; operator_name:string; operator_contact:string; digest:string;
  suppliers:{supplier_public_id:string;name:string;contact:string;items:{product_public_id:string;sku_code:string;product_name:string;sku_name:string;quantity:number}[]}[];
}
export function parseFulfillment(value:unknown):FulfillmentNotice {
  const x=value as FulfillmentNotice;
  const text=(v:unknown,n=500)=>typeof v==='string'&&v.length<=n;
  if(!x || typeof x.ready!=='boolean'||!text(x.message)||!text(x.notice)||!text(x.operator_name,200)||!text(x.operator_contact,200)||
    !Array.isArray(x.suppliers)||x.suppliers.length>20||typeof x.digest!=='string'||
    (x.ready&&(!/^[a-f0-9]{64}$/.test(x.digest)||!x.operator_name||!x.operator_contact||!x.suppliers.length))||
    (!x.ready&&(x.digest!==''||x.suppliers.length!==0))||x.suppliers.some(s=>!s||!text(s.supplier_public_id,32)||!s.supplier_public_id||!text(s.name,200)||!s.name||!text(s.contact,200)||!s.contact||
      !Array.isArray(s.items)||!s.items.length||s.items.length>20||s.items.some(i=>!i||!text(i.product_public_id,32)||!text(i.sku_code,100)||!text(i.product_name,200)||!text(i.sku_name,160)||!Number.isSafeInteger(i.quantity)||i.quantity<1)))
      throw new MemberSessionError('发货服务信息暂不可用');
  return x;
}
export async function getFulfillment(items:CartDisplayLine[]):Promise<FulfillmentNotice> {
  return parseFulfillment(await requestMemberPrivate('/orders/fulfillment-preview','POST',{
    lines:items.map(x=>({product_public_id:x.product_public_id,sku_code:x.sku_code,quantity:x.quantity,expected_points_price:x.points_price}))}));
}
