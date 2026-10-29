// @ts-nocheck
import { CATEGORY_ENTRIES } from './services/category-navigation';
// OFFLINE UI FIXTURES ONLY. Never imported by the production mini program.
// Shift UTC to Beijing wall time before attaching the +08:00 offset.
const stamp=()=>new Date(Date.now()+8*60*60*1000).toISOString().slice(0,19)+'+08:00';
const copy=x=>JSON.parse(JSON.stringify(x));
const address={address_public_id:'ADDR-'+'d'.repeat(32),is_default:true,recipient_name:'演示收件人',phone:'13800000000',province:'演示省',city:'演示市',district:'演示区',detail:'虚拟地址，请勿用于真实配送'};
const products=[
 ['PRD-DEMO-001','晨光陶瓷杯','把日常泡成好时光','home','120.00','雾白 · 350ml'],
 ['PRD-DEMO-002','轻柔棉织毛巾','柔软亲肤，陪伴每一天','home','80.00','鼠尾草绿 · 双条装'],
 ['PRD-DEMO-003','元气果干礼盒','一份甜蜜的小心意','food','180.00','综合口味 · 6袋装'],
 ['PRD-DEMO-004','田园鲜果组合','为餐桌添一份清新','fresh','160.00','演示组合 · 1盒'],
 ['PRD-DEMO-005','轻行帆布包','装下出门的小欢喜','bags','150.00','米白 · 标准款'],
 ['PRD-DEMO-006','桌面小音箱','给生活添一点旋律','digital','260.00','森林绿 · 标准款'],
 ['PRD-DEMO-007','随身护手霜','细心照顾每一天','beauty','60.00','清香 · 30g'],
 ['PRD-DEMO-008','日常小炖锅','煮一餐温暖好味道','kitchen','220.00','浅绿 · 1.5L'],
 ['PRD-DEMO-009','软绵小熊玩偶','陪伴每个小小日常','kids','100.00','浅棕 · 25cm'],
].map((r,i)=>({product_public_id:r[0],name:r[1],subtitle:r[2],category_slug:r[3],min_points_price:r[4],in_stock:true,
 main_image_url:`/uploads/mall_products/${r[0]}/main.png`,description:'此商品仅用于本机界面验收，不代表真实在售商品。可体验规格选择、购物车及兑换流程。',
 images:[{role:'main',url:`/uploads/mall_products/${r[0]}/main.png`,alt_text:r[1]}],
 skus:[{sku_code:'SKU-DEMO-'+(i+1),name:r[5],points_price:r[4],in_stock:true,stock_status:'IN_STOCK'}]}));
let addresses=[copy(address)], orders=[], next=1, scenario='normal', available=1200, reserved=0;
let ledger=[{entry_type:'GRANT',available_points_delta:'1200.00',reserved_points_delta:'0.00',created_at:stamp()}];
let tokenValid=true;
const money=n=>n.toFixed(2);
function movement(type,a,r){available+=a;reserved+=r;ledger.unshift({entry_type:type,available_points_delta:money(a),reserved_points_delta:money(r),created_at:stamp()});}
export function setScenario(value){scenario=value;tokenValid=true;}
export function demoState(){return {scenario,available:money(available),orders:copy(orders)};}
export function resetDemo(){addresses=[copy(address)];orders=[];next=1;scenario='normal';available=1200;reserved=0;tokenValid=true;ledger=[{entry_type:'GRANT',available_points_delta:'1200.00',reserved_points_delta:'0.00',created_at:stamp()}];}
export function advance(id){const o=orders.find(x=>x.order_public_id===id);if(!o)return;
 const total=Number(o.total_points);
 if(o.status==='CREATED'){o.status='FULFILLING';movement('CONSUME',0,-total);}
 else if(o.status==='FULFILLING'){o.status='SHIPPED';o.shipping_carrier='演示物流';o.tracking_number='DEMO-NOT-A-REAL-PARCEL';}
 else if(o.status==='SHIPPED')o.status='COMPLETED';
 else if(o.status==='COMPLETED'){o.status='REFUNDED';movement('REFUND',total,0);}
}
export function clearDemoAddress(id){const o=orders.find(x=>x.order_public_id===id);if(o&&['COMPLETED','REFUNDED','CANCELLED'].includes(o.status)){o.address=null;o.address_state='CLEARED';}}
export function notice(lines){const groups={};for(const line of lines){const p=products.find(p=>p.product_public_id===line.product_public_id);if(!p)throw Error();
 const key=p.category_slug==='food'?'SUP-DEMO-B':'SUP-DEMO-A';
 const group=groups[key]||(groups[key]={supplier_public_id:key,name:key==='SUP-DEMO-A'?'演示家居供应商':'演示食品供应商',contact:'demo@example.invalid',items:[]});
 group.items.push({product_public_id:p.product_public_id,sku_code:line.sku_code,product_name:p.name,sku_name:p.skus[0].name,quantity:line.quantity});}
 return {ready:scenario!=='missing_supplier',message:scenario==='missing_supplier'?'部分商品发货资料待完善':'',operator_name:'开礼优选 · 演示公司',operator_contact:'demo@example.invalid',notice:'您向本公司兑换商品，由受托供应商发货。仅提供本订单配送所必需的信息。此处为虚拟资料。',suppliers:scenario==='missing_supplier'?[]:Object.values(groups),digest:scenario==='missing_supplier'?'':'f'.repeat(64)};}
function response(code,data){return {statusCode:code,data:copy(data)};}
export function dispatch(options){
 const full=options.url.split('/api/miniprogram/v1')[1];if(!full)throw Error('Unexpected demo route');
 const path=full.split('?')[0], method=options.method||'GET', body=options.data||{};
 const params={};(full.split('?')[1]||'').split('&').filter(Boolean).forEach(v=>{const [k,x]=v.split('=');params[k]=decodeURIComponent(x||'');});
 if(scenario==='offline')throw Error('simulated offline');
 if(path==='/categories')return response(200,CATEGORY_ENTRIES.map(item=>({name:item.name,slug:item.key})));
 if(path==='/products'){let rows=scenario==='empty'?[]:products.filter(p=>(!params.category||p.category_slug===params.category)&&(!params.q||p.name.includes(params.q)));const page=Number(params.page||1);return response(200,{items:rows.slice((page-1)*20,page*20),total:rows.length,page,page_size:20});}
 if(path.startsWith('/products/')){
  if(scenario==='detail_offline_once'){scenario='normal';throw Error('simulated detail network failure');}
  const p=products.find(x=>x.product_public_id===path.split('/').pop());
  if(p&&scenario==='detail_images_missing')return response(200,{...p,images:[
   {role:'main',url:`/uploads/mall_products/${p.product_public_id}/missing-main.png`,alt_text:p.name},
   {role:'detail',url:`/uploads/mall_products/${p.product_public_id}/missing-detail.png`,alt_text:p.name}]});
  if(p&&scenario==='detail_long_text')return response(200,{...p,name:'舒适生活系列多用途随身日常用品 '+ 'LONGNAME'.repeat(12),
   subtitle:'此长文案仅用于窄屏换行验收',description:'Specification'.repeat(24),
   skus:p.skus.map(s=>({...s,name:'演示长规格名称 '+ 'LONGSKU'.repeat(18),points_price:'123456789.99'}))});
  return response(p?200:404,p||{});
 }
 if(path==='/auth/wechat-session'){tokenValid=true;return response(200,{token_type:'Bearer',token:'demo.session',expires_in:900});}
 if(path==='/activation/redeem')return response(200,{status:'activated'});
 if(scenario==='expired'||!tokenValid)return response(401,{detail:'演示会话失效'});
 if(path==='/auth/me')return response(200,{member_public_id:'MEM-DEMO-001'});
 if(path==='/points/summary')return response(200,{available_points:money(available),reserved_points:money(reserved),grant_count:1,nearest_expires_at:'2027-10-28T23:59:59+08:00'});
 if(path.startsWith('/points/')){const page=Number(params.page||1),rows=path.endsWith('ledger')?(params.available_only==='true'?ledger.filter(e=>Number(e.available_points_delta)!==0):ledger):[{granted_points:'1200.00',available_points:money(available),reserved_points:money(reserved),activated_at:'2026-10-28T09:00:00+08:00',expires_at:'2027-10-28T23:59:59+08:00',status:'ACTIVE'}];return response(200,{items:rows.slice((page-1)*20,page*20),total:rows.length,page,page_size:20});}
 if(path==='/addresses'&&method==='GET')return response(200,{items:addresses,total:addresses.length,limit:20});
 if(path==='/addresses'&&method==='POST'){if(addresses.length>=20)return response(409,{});const x={...body,address_public_id:'ADDR-'+String(next++).padStart(32,'0'),is_default:!addresses.length};addresses.push(x);return response(201,x);}
 if(path.startsWith('/addresses/')){const id=path.split('/')[2],x=addresses.find(a=>a.address_public_id===id);if(!x)return response(404,{});
 if(method==='DELETE'){addresses=addresses.filter(a=>a!==x);return response(204,null);}if(path.endsWith('/default')){addresses.forEach(a=>a.is_default=a===x);return response(200,x);}Object.assign(x,body);return response(200,x);}
 if(path==='/orders/fulfillment-preview')return response(200,notice(body.lines));
 if(path==='/orders'&&method==='POST'){
  const old=orders.find(o=>o.request_id===body.request_id);if(old)return response(200,old);
  if(['price_conflict','insufficient','missing_supplier'].includes(scenario))return response(409,{detail:'演示条件变化'});
  const items=body.lines.map(l=>{const p=products.find(p=>p.product_public_id===l.product_public_id);return {product_name:p.name,sku_name:p.skus[0].name,quantity:l.quantity,unit_points_price:p.skus[0].points_price,line_points:money(Number(p.skus[0].points_price)*l.quantity)};});
  const total=items.reduce((n,i)=>n+Number(i.line_points),0);if(total>available)return response(409,{});
  const o={order_public_id:'ORD-'+String(next++).padStart(16,'0'),request_id:body.request_id,status:'CREATED',total_points:money(total),total_quantity:items.reduce((n,i)=>n+i.quantity,0),created_at:stamp(),address:copy(body.expected_address),address_state:'AVAILABLE',fulfillment:notice(body.lines),items,shipping_carrier:null,tracking_number:null};orders.unshift(o);movement('RESERVE',-total,total);
  if(scenario==='timeout_after_submit'){scenario='normal';throw Error('simulated lost response');}return response(201,o);
 }
 if(path==='/orders'){const page=Number(params.page||1);return response(200,{items:orders.slice((page-1)*20,page*20),total:orders.length,page,page_size:20});}
 if(path.startsWith('/orders/')){const parts=path.split('/');const o=parts[2]==='request'?orders.find(x=>x.request_id===parts[3]):orders.find(x=>x.order_public_id===parts[2]);if(!o)return response(404,{});
 if(path.endsWith('/cancel')){if(o.status==='CREATED'){o.status='CANCELLED';movement('RELEASE',Number(o.total_points),-Number(o.total_points));}else if(o.status!=='CANCELLED')return response(409,{});}return response(200,o);}
 return response(404,{});
}
export function demoRequest(options){setTimeout(()=>{try{options.success(dispatch(options));}catch(_){options.fail({errMsg:'offline UI simulation'});}},120);}
export function demoLogin(options){setTimeout(()=>options.success({code:'OFFLINE-DEMO-CODE'}),50);}
