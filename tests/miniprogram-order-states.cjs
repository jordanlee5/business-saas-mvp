// Offline order-list state checks. No real WeChat or database calls.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]),demoRoot=path.resolve(process.argv[3]);
let definition,signedIn=true,requests=[];
global.wx={getAccountInfoSync:()=>({miniProgram:{appId:'touristappid',envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'}),request:()=>{throw Error('Native transport forbidden');},login:()=>{throw Error('Native login forbidden');}};
global.Page=x=>definition=x;
const session=require(path.join(root,'services/member-session.js')),orders=require(path.join(root,'services/orders.js'));
session.hasMemberSession=()=>signedIn;
orders.listOrders=page=>new Promise((resolve,reject)=>requests.push({page,resolve,reject}));
const row={order_public_id:'ORD-0000000000000001',status:'CREATED',total_points:'120.00',total_quantity:1,created_at:'2026-10-29T12:00:00+08:00'};
orders.getOrder=async()=>({...row,address:null,items:[],shipping_carrier:null,tracking_number:null});
require(path.join(root,'pages/orders/index.js'));const original=definition;
function page(d=original){return {...d,data:structuredClone(d.data),setData(x){Object.assign(this.data,x);}};}
const flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve();};
async function success(p,num=1,total=1,items=[row]){const pending=p.load(num);requests.shift().resolve({items,total,page:num,page_size:20});await pending;}
let count=0;
async function test(name,run){signedIn=true;await run();assert.equal(requests.length,0);count++;console.log('PASS '+name);}
(async()=>{
 await test('guest never displays a confirmed empty list or requests member data',async()=>{
  signedIn=false;const p=page();await p.load(1);assert.equal(p.data.hasSession,false);assert.equal(p.data.listReady,false);assert.equal(p.data.total,0);
 });
 await test('successful empty and single-page lists become ready only after response',async()=>{
  const p=page();const pending=p.load(1);assert.equal(p.data.listReady,false);assert.equal(p.data.loading,true);requests.shift().resolve({items:[],total:0,page:1,page_size:20});await pending;
  assert.equal(p.data.listReady,true);assert.equal(p.data.total,0);assert.equal(p.data.items.length,0);await success(p);assert.equal(p.data.items.length,1);assert.equal(p.data.listReady,true);
 });
 await test('failed next page clears stale count and retries exactly that page once',async()=>{
  const p=page();await success(p,1,21);p.onNext();assert.equal(requests[0].page,2);assert.equal(p.data.page,2);assert.equal(p.data.total,0);assert.equal(p.data.listReady,false);
  requests.shift().reject(Error('offline'));await flush();assert.equal(p.data.error,'offline');assert.equal(p.data.items.length,0);
  p.onNext();p.onPrevious();assert.equal(requests.length,0);p.onReload();p.onReload();assert.equal(requests.length,1);assert.equal(requests[0].page,2);
  requests.shift().resolve({items:[row],total:21,page:2,page_size:20});await flush();assert.equal(p.data.page,2);assert.equal(p.data.listReady,true);assert.equal(p.data.error,'');
 });
 await test('hiding during a request clears items, total and readiness and ignores late response',async()=>{
  const p=page();await success(p,1,21);const pending=p.load(2),request=requests.shift();p.onHide();request.resolve({items:[row],total:21,page:2,page_size:20});await pending;
  assert.equal(p.data.total,0);assert.equal(p.data.listReady,false);assert.deepEqual(p.data.items,[]);assert.equal(p.data.hasSession,false);
 });
 await test('session expiry during reload cannot retain old count or pagination',async()=>{
  const p=page();await success(p,1,21);const pending=p.load(2);signedIn=false;requests.shift().reject(Error('expired'));await pending;
  assert.equal(p.data.hasSession,false);assert.equal(p.data.total,0);assert.equal(p.data.listReady,false);assert.deepEqual(p.data.items,[]);
 });
 await test('older list response cannot replace a newer request or its page number',async()=>{
  const p=page();const first=p.load(1),old=requests.shift(),second=p.load(2),latest=requests.shift();latest.resolve({items:[row],total:21,page:2,page_size:20});await second;old.resolve({items:[],total:0,page:1,page_size:20});await first;
  assert.equal(p.data.page,2);assert.equal(p.data.total,21);assert.equal(p.data.listReady,true);
 });
 await test('detail returns to the same list page; empty later page remains navigable; invalid pages are ignored',async()=>{
  const p=page();await success(p,2,21);await p.show(row.order_public_id);assert.equal(p.data.listReady,false);assert.equal(p.data.total,0);p.onBack();assert.equal(requests[0].page,2);
  requests.shift().resolve({items:[],total:20,page:2,page_size:20});await flush();assert.equal(p.data.page,2);assert.equal(p.data.listReady,true);p.onPrevious();assert.equal(requests[0].page,1);requests.shift().resolve({items:[row],total:20,page:1,page_size:20});await flush();
  for(const value of [0,-1,1.5,NaN,Infinity])await p.load(value);assert.equal(requests.length,0);
 });
 await test('generated demo order-list failure recovers to genuine empty state',async()=>{
  const demoSession=require(path.join(demoRoot,'services/member-session.js')),demo=require(path.join(demoRoot,'demo-transport.js'));
  await demoSession.loginMember();demo.setScenario('orders_offline_once');require(path.join(demoRoot,'pages/orders/index.js'));const p=page(definition);
  await p.load(1);assert.ok(p.data.error);assert.equal(p.data.listReady,false);p.onReload();
  for(let i=0;i<100&&p.data.loading;i++)await new Promise(r=>setTimeout(r,10));
  assert.equal(p.data.loading,false);assert.equal(p.data.error,'');assert.equal(p.data.listReady,true);assert.equal(p.data.total,0);
 });
 console.log(`PASS ${count} order list state groups (offline)`);
})().catch(e=>{console.error(e);process.exitCode=1;});
