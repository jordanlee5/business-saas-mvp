// Offline transport/page checks; not WeChat platform acceptance.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]);let requests=[],definition;
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop',appId:'touristappid'}}),getSystemInfoSync:()=>({platform:'devtools'}),login:o=>o.success({code:'test'}),request:o=>requests.push(o),reLaunch:()=>{}};
global.Page=o=>definition=o;
require(path.join(root,'services/privacy.js')).assertPrivacyAuthorized=async()=>{};
const session=require(path.join(root,'services/member-session.js'));
require(path.join(root,'pages/points/index.js'));
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const reply=(statusCode,data)=>{assert.ok(requests.length);requests.shift().success({statusCode,data});};
const row=(entry_type,available_points_delta,reserved_points_delta='0.00')=>({entry_type,available_points_delta,reserved_points_delta,created_at:'2026-10-29T12:34:56+08:00'});
const rows=[row('EXPIRE','-1.00'),row('REFUND','4.00'),row('RELEASE','2.00','-2.00'),row('RESERVE','-2.00','2.00'),row('ADJUST','-1.25'),row('GRANT','12.50')];
const ledger=(items=rows,total=items.length,page=1)=>({items,total,page,page_size:20});
const summary={available_points:'14.25',reserved_points:'2.00',grant_count:8,nearest_expires_at:'2027-10-28T23:59:59+08:00'};
async function login(){const p=session.loginMember();await flush();reply(200,{token_type:'Bearer',token:'test.signature',expires_in:900});await flush();reply(200,{member_public_id:'MEM-1'});await p;}
function page(){return {...definition,data:structuredClone(definition.data),setData(x){Object.assign(this.data,x);}};}
async function show(p,items=rows,total=items.length,expiry=summary.nearest_expires_at){p.onShow();await flush();reply(200,{member_public_id:'MEM-1'});await flush();assert.ok(requests[0].url.endsWith('/points/summary'));reply(200,{...summary,nearest_expires_at:expiry});await flush();assert.ok(requests[0].url.endsWith('/points/ledger?page=1&page_size=20&available_only=true'));reply(200,ledger(items,total));await flush();}
let count=0;
async function test(name,fn){await fn();assert.equal(requests.length,0);count++;console.log('PASS '+name);}
(async()=>{
 await test('guest does not request private points',async()=>{const p=page();p.onShow();await flush();assert.equal(p.data.signedIn,false);});
 await login();
 await test('plain labels and signed amounts without batch or reserved display data',async()=>{
  const p=page();await show(p);assert.equal(p.data.availablePoints,'14.25');
  assert.deepEqual(p.data.ledgerItems.map(x=>x.title),['积分到期','退款退回','订单取消退回','商品兑换','积分调整','积分到账']);
  assert.deepEqual(p.data.ledgerItems.map(x=>x.amount),['-1.00','+4.00','+2.00','-2.00','-1.25','+12.50']);
  for(const key of ['reservedPoints','grantCount','grantItems','memberPublicId'])assert.equal(key in p.data,false);
  assert.equal(p.data.nearestExpiry,'2027-10-28 23:59:59');p.onHide();assert.equal(p.data.availablePoints,'');assert.deepEqual(p.data.ledgerItems,[]);
 });
 await test('empty and no expiry remain valid',async()=>{const p=page();await show(p,[],0,null);assert.equal(p.data.signedIn,true);assert.equal(p.data.ledgerTotal,0);assert.equal(p.data.nearestExpiry,'');assert.equal(p.data.ledgerHasMore,false);});
 await test('filtered pages append once, repeated taps do not duplicate request',async()=>{
  const p=page();await show(p,Array.from({length:20},()=>row('GRANT','1.00')),21);
  const next=p.onMoreLedgerTap();p.onMoreLedgerTap();assert.equal(requests.length,1);assert.ok(requests[0].url.includes('page=2&page_size=20&available_only=true'));
  reply(200,ledger([row('RESERVE','-1.00')],21,2));await next;assert.equal(p.data.ledgerItems.length,21);assert.equal(p.data.ledgerHasMore,false);
 });
 await test('hidden page ignores late page response',async()=>{
  const p=page();await show(p,Array.from({length:20},()=>row('GRANT','1.00')),21);const next=p.onMoreLedgerTap();p.onHide();reply(200,ledger([row('REFUND','1.00')],21,2));await next;assert.equal(p.data.signedIn,false);assert.equal(p.data.ledgerItems.length,0);
 });
 await test('zero change, malformed amounts and inconsistent filtered pagination fail closed',async()=>{
  for(const data of [ledger([row('CONSUME','0.00','-2.00')]),ledger([row('REFUND','bad')]),ledger([],1),ledger([row('GRANT','1.00')],0)]){
   const pending=session.getCurrentMemberPointsLedger(1,true);reply(200,data);await assert.rejects(pending);
  }
  const legacy=session.getCurrentMemberPointsLedger(1);reply(200,ledger([row('CONSUME','0.00','-2.00')]));assert.equal((await legacy).total,1);
 });
 await test('expired session clears displayed account and records',async()=>{
  const p=page();await show(p,Array.from({length:20},()=>row('GRANT','1.00')),21);const next=p.onMoreLedgerTap();reply(401,{});await next;assert.equal(session.hasMemberSession(),false);assert.equal(p.data.hasSession,false);assert.equal(p.data.availablePoints,'');assert.equal(p.data.ledgerItems.length,0);
 });
 await login();
 await test('old session response cannot enter a new account',async()=>{
  const pending=session.getCurrentMemberPointsLedger(1,true);const old=requests.shift();session.clearMemberSession();await login();old.success({statusCode:200,data:ledger()});await assert.rejects(pending,e=>e.status===401);
 });
 await test('leaving while summary loads cannot restore hidden records',async()=>{
  const p=page();p.onShow();await flush();reply(200,{member_public_id:'MEM-1'});await flush();p.onHide();reply(200,summary);await flush();assert.equal(p.data.availablePoints,'');assert.equal(p.data.signedIn,false);
 });
 console.log(`PASS ${count} points groups (offline)`);
})().catch(e=>{console.error(e);process.exitCode=1;});
