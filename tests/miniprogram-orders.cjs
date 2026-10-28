const assert=require('node:assert/strict');const path=require('node:path');
const root=path.resolve(process.argv[2]);let requests=[],storage=new Map(),definition;
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop',appId:'touristappid'}}),getSystemInfoSync:()=>({platform:'devtools'}),login:o=>o.success({code:'test'}),request:o=>requests.push(o),getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,structuredClone(v)),navigateTo:()=>{},reLaunch:()=>{}};
global.Page=o=>definition=o;
require(path.join(root,'services/privacy.js')).assertPrivacyAuthorized=async()=>{};
const session=require(path.join(root,'services/member-session.js'));
const service=require(path.join(root,'services/orders.js'));
const cart=require(path.join(root,'services/cart.js'));
require(path.join(root,'services/fulfillment.js')).getFulfillment=async()=>({ready:true,digest:'f'.repeat(64),suppliers:[],notice:'test',operator_name:'test',operator_contact:'test',message:''});
const flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve();};
const reply=(statusCode,data)=>{assert.ok(requests.length);requests.shift().success({statusCode,data});};
const addr={recipient_name:'收件测试',phone:'13800000001',province:'测试省',city:'测试市',district:'测试区',detail:'测试路1号'};
const id='ADDR-'+'a'.repeat(32);
const line={product_public_id:'PRD-TEST',sku_code:'SKU-1',quantity:2,key:'PRD-TEST:SKU-1',product_name:'商品',sku_name:'规格',points_price:'40.00',line_points:'80.00',available:true,status:'有货'};
const order={order_public_id:'ORD-ABCDEFGHJKLMNPQR',status:'CREATED',total_points:'80.00',total_quantity:2,created_at:'2026-10-28T12:00:00+08:00',address:addr,items:[{product_name:'商品',sku_name:'规格',quantity:2,unit_points_price:'40.00',line_points:'80.00'}],shipping_carrier:null,tracking_number:null};
async function login(){const p=session.loginMember();await flush();reply(200,{token_type:'Bearer',token:'test.signature',expires_in:900});await flush();reply(200,{member_public_id:'MEM-1'});await p;}
function page(){return {...definition,data:structuredClone(definition.data),generation:0,setData(x){Object.assign(this.data,x);}};}
cart.readCart=()=>storage.get('cart')||[];
cart.loadCartDisplay=async()=>({items:[line],estimate:'80.00',hasUnavailable:false});
cart.removeCartLine=()=>storage.set('cart',[]);
require(path.join(root,'pages/checkout/index.js'));
async function load(p){p.onShow();await flush();reply(200,{member_public_id:'MEM-1'});await flush();if(requests.length){reply(200,{items:[{...addr,address_public_id:id,is_default:true}],total:1,limit:20});await flush();}}
let count=0;
async function test(name,fn){storage.clear();await fn();assert.equal(requests.length,0);count++;console.log('PASS '+name);}
(async()=>{
 await login();
 await test('only correlation key and public cart lines persist',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const value=JSON.stringify([...storage.values()]);
  for(const privateValue of [addr.recipient_name,addr.phone,addr.detail,'Bearer'])assert.equal(value.includes(privateValue),false);
  assert.equal(service.pendingCheckout('MEM-2'),'');assert.equal(service.canRetryCheckout('MEM-2'),false);
 });
 await test('retry preserves exact body and key',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);let p=service.submitCheckout('MEM-1');const first=requests[0];requests.shift().fail({});await assert.rejects(p);
  p=service.submitCheckout('MEM-1');assert.deepEqual(requests[0].data,first.data);reply(200,order);assert.equal((await p).order_public_id,order.order_public_id);
 });
 await test('unresolved records isolated between members',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const first=service.pendingCheckout('MEM-1');service.beginCheckout('MEM-2',id,addr,[line]);assert.equal(service.pendingCheckout('MEM-1'),first);service.clearPendingCheckout('MEM-2');assert.equal(service.pendingCheckout('MEM-1'),first);
 });
 await test('malformed successful response cannot be treated as rejection',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const p=service.submitCheckout('MEM-1');reply(201,{...order,address:{...addr,phone:'bad'}});await assert.rejects(p,e=>e.status===0);assert.ok(service.pendingCheckout('MEM-1'));
 });
 await test('unknown outcome recovered by request lookup',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const p=service.resolveCheckout('MEM-1');assert.ok(requests[0].url.includes('/orders/request/'));reply(200,order);assert.equal((await p).total_points,'80.00');
 });
 await test('order list and cancel responses validated',async()=>{
  let p=service.listOrders(1);reply(200,{items:[order],total:1,page:1,page_size:20});assert.equal((await p).items.length,1);
  p=service.cancelOrder(order.order_public_id);assert.ok(requests[0].url.endsWith('/cancel'));reply(200,{...order,status:'CANCELLED'});assert.equal((await p).status,'CANCELLED');
 });
 await test('double click submits only once and clears purchased cart',async()=>{
  storage.set('cart',[line]);const p=page();await load(p);p.onSubmit();p.onSubmit();assert.equal(requests.length,1);reply(201,order);await flush();assert.equal(p.data.complete,order.order_public_id);assert.equal(cart.readCart().length,0);assert.equal(service.pendingCheckout('MEM-1'),'');
 });
 await test('timeout blocks new order and allows same-key retry',async()=>{
  const p=page();await load(p);p.onSubmit();const key=requests[0].data.request_id;requests.shift().fail({});await flush();assert.equal(p.data.pending,true);
  p.onSubmit();assert.equal(requests[0].data.request_id,key);reply(200,order);await flush();assert.ok(p.data.complete);
 });
 await test('switch page during submit clears PII and consumes success safely',async()=>{
  storage.set('cart',[line]);const p=page();await load(p);p.onSubmit();p.onSubmit();const request=requests.shift();p.onHide();p.onShow();await flush();assert.equal(requests.length,0);assert.deepEqual(p.data.addresses,[]);
  request.success({statusCode:201,data:order});await flush();reply(200,{member_public_id:'MEM-1'});await flush();assert.equal(p.data.complete,order.order_public_id);assert.equal(p.data.busy,false);assert.equal(cart.readCart().length,0);
 });
 await test('price conflict preserves key for a corrected confirmation',async()=>{
  const p=page();await load(p);p.onSubmit();reply(409,{detail:'PRIVATE'});await flush();assert.equal(p.data.pending,true);const key=service.pendingCheckout('MEM-1');assert.ok(key);assert.equal(p.data.error.includes('PRIVATE'),false);service.beginCheckout('MEM-1',id,addr,[{...line,points_price:'41.00'}]);assert.equal(service.pendingCheckout('MEM-1'),key);
 });
 await test('restart-style review rebuilds payload using original key',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const key=service.pendingCheckout('MEM-1');service.beginCheckout('MEM-2',id,addr,[line]);
  const p=page();p.load(true);await flush();reply(200,{member_public_id:'MEM-1'});await flush();reply(200,{items:[{...addr,address_public_id:id,is_default:true}],total:1,limit:20});await flush();
  p.onSubmit();assert.equal(requests[0].data.request_id,key);reply(201,order);await flush();assert.equal(p.data.complete,order.order_public_id);
 });
 require(path.join(root,'pages/orders/index.js'));
 await test('order detail late response cannot restore hidden personal data',async()=>{
  const p=page();p.onShow();reply(200,{items:[order],total:1,page:1,page_size:20});await flush();p.show(order.order_public_id);p.onHide();reply(200,order);await flush();assert.equal(p.data.detail,null);
 });
 await test('member cancellation requires confirmation and blocks duplicate clicks',async()=>{
  const p=page();p.onShow();reply(200,{items:[order],total:1,page:1,page_size:20});await flush();p.show(order.order_public_id);reply(200,order);await flush();
  p.onConfirmCancel();assert.equal(requests.length,0);p.onCancel();p.onConfirmCancel();p.onConfirmCancel();assert.equal(requests.length,1);reply(200,{...order,status:'CANCELLED'});await flush();assert.equal(p.data.detail.status,'CANCELLED');
 });
 await test('session expiry preserves unresolved correlation without leaking old response',async()=>{
  service.beginCheckout('MEM-1',id,addr,[line]);const p=service.submitCheckout('MEM-1');session.clearMemberSession();reply(201,order);await assert.rejects(p,e=>e.status===401);assert.ok(service.pendingCheckout('MEM-1'));
 });
 console.log(`Order client simulated checks: ${count} PASS; not WeChat platform acceptance.`);
})().catch(e=>{console.error(e);process.exitCode=1;});
