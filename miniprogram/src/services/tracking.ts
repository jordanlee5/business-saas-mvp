import {requestMemberPrivate,MemberSessionError} from './member-session';
export const trackingLabels:Record<string,string>={PENDING:'暂无运输轨迹',IN_TRANSIT:'运输中',OUT_FOR_DELIVERY:'派送中',DELIVERED:'已签收',EXCEPTION:'物流异常'};
export interface Tracking {parcel_id:number;status:string;availability:string;source:string;updated_at:string|null;events:{time:string;description:string}[]}
function time(x:unknown):boolean{return typeof x==='string'&&/\+08:00$/.test(x)&&Number.isFinite(Date.parse(x));}
export async function getTracking(order:string,parcel:number):Promise<Tracking>{
 if(!/^ORD-[A-Z0-9]{16}$/.test(order)||!Number.isSafeInteger(parcel)||parcel<1||parcel>2147483647)throw new MemberSessionError('物流链接无效');
 const x=await requestMemberPrivate('/orders/'+order+'/parcels/'+parcel+'/tracking','GET') as Tracking;
 if(!x||x.parcel_id!==parcel||!trackingLabels[x.status]||!['LIVE','CACHED','STALE','UNAVAILABLE','NOT_CONFIGURED'].includes(x.availability)||
    typeof x.source!=='string'||x.source.length>80||!(x.updated_at===null||time(x.updated_at))||!Array.isArray(x.events)||x.events.length>100||
    x.events.some(e=>!e||!time(e.time)||typeof e.description!=='string'||!e.description.trim()||e.description.length>500)||
    (['LIVE','CACHED','STALE'].includes(x.availability)&&(!x.updated_at||!x.source))||
    (['UNAVAILABLE','NOT_CONFIGURED'].includes(x.availability)&&(x.events.length>0||x.updated_at!==null)))throw new MemberSessionError('物流数据暂不可用，请稍后重试');
 return {parcel_id:x.parcel_id,status:x.status,availability:x.availability,source:x.source,updated_at:x.updated_at,events:x.events.map(e=>({time:e.time,description:e.description}))};
}
