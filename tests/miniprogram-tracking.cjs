// Compiled production transport + page, with only native WeChat I/O simulated.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]);let definition,requests=[],stops=0;
global.Page=x=>definition=x;
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop',appId:'touristappid'}}),getSystemInfoSync:()=>({platform:'devtools'}),
 login:x=>x.success({code:'offline'}),request:x=>requests.push(x),stopPullDownRefresh:()=>stops++,redirectTo:()=>{},reLaunch:()=>{}};
require(path.join(root,'services/privacy.js')).assertPrivacyAuthorized=async()=>{};
const session=require(path.join(root,'services/member-session.js')),api=require(path.join(root,'services/tracking.js'));
require(path.join(root,'pages/tracking/index.js'));
const id='ORD-0000000000000001';
const result=(parcel=1,availability='LIVE')=>({parcel_id:parcel,status:'IN_TRANSIT',availability,source:'虚拟快递',updated_at:'2026-11-06T10:00:00+08:00',events:[{time:'2026-11-06T09:00:00+08:00',description:'测试揽收'}]});
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const reply=(statusCode,data)=>{assert.ok(requests.length);requests.shift().success({statusCode,data});};
async function login(){const p=session.loginMember();await flush();reply(200,{token_type:'Bearer',token:'test.signature',expires_in:900});await flush();reply(200,{member_public_id:'MEM-TEST'});await p;}
function page(){const p={...definition,data:structuredClone(definition.data),setData(x){Object.assign(this.data,x);}};p.onLoad({order:id,parcel:'1'});return p;}
let count=0;async function test(name,run){await run();assert.equal(requests.length,0);count++;console.log('PASS '+name);}
(async()=>{
 await login();
 await test('actual private transport includes bearer and only the selected parcel',async()=>{
  const p=api.getTracking(id,2);assert.ok(requests[0].url.endsWith('/'+id+'/parcels/2/tracking'));assert.equal(requests[0].method,'GET');assert.equal(requests[0].header.Authorization,'Bearer test.signature');reply(200,result(2));assert.equal((await p).parcel_id,2);
 });
 await test('rejects malformed routes without network access',async()=>{
  for(const [order,parcel] of [[id,0],[id,-1],[id,2.5],[id,2147483648],['../other',1]])await assert.rejects(api.getTracking(order,parcel));
 });
 await test('valid response renders Beijing times and duplicate taps do not query twice',async()=>{
  const p=page(),pending=p.load();p.onRetry();assert.equal(requests.length,1);reply(200,result());await pending;assert.equal(p.data.events[0].description,'测试揽收');assert.ok(p.data.events[0].timeText.includes('09:00'));assert.equal(p.data.loading,false);
 });
 await test('wrong parcel or malformed provider payload rejected',async()=>{
  for(const x of [result(2),{...result(),events:[{time:'yesterday',description:'bad'}]},{...result(),availability:'unknown'},{...result(),availability:'UNAVAILABLE'}]){const p=api.getTracking(id,1);reply(200,x);await assert.rejects(p);}
 });
 await test('stale and no-events states remain distinguishable',async()=>{
  const p=page();let pending=p.load();reply(200,result(1,'STALE'));await pending;assert.equal(p.data.tracking.availability,'STALE');
  pending=p.load();reply(200,{...result(),status:'PENDING',events:[]});await pending;assert.equal(p.data.tracking.status,'PENDING');assert.deepEqual(p.data.events,[]);
 });
 await test('provider unavailable and disabled do not fabricate events',async()=>{
  for(const availability of ['UNAVAILABLE','NOT_CONFIGURED']){const p=page(),pending=p.load();reply(200,{parcel_id:1,availability,status:'PENDING',events:[],source:'',updated_at:null});await pending;assert.equal(p.data.tracking.availability,availability);assert.deepEqual(p.data.events,[]);}
 });
 await test('network failure can recover with pull refresh',async()=>{
  const p=page(),first=p.load();requests.shift().fail({});await first;assert.ok(p.data.error);const second=p.onPullDownRefresh();reply(200,result());await second;assert.equal(p.data.error,'');assert.equal(stops,1);assert.equal(p.data.events.length,1);
 });
 await test('hiding page clears data and ignores late private response',async()=>{
  const p=page(),pending=p.load();p.onHide();reply(200,result());await pending;assert.equal(p.data.tracking,null);assert.deepEqual(p.data.events,[]);
 });
 await test('logout during request discards response',async()=>{
  const p=page(),pending=p.load();session.clearMemberSession();reply(200,result());await pending;assert.equal(p.data.tracking,null);assert.equal(p.data.hasSession,false);
 });
 await test('guest and invalid page never query',async()=>{
  const guest=page();await guest.load();assert.equal(guest.data.hasSession,false);await login();const invalid=page();invalid.onLoad({order:'bad',parcel:'1'});await invalid.load();assert.ok(invalid.data.error);
 });
 await test('forbidden response clears prior page data and no fallback to another parcel',async()=>{
  const p=page();let pending=p.load();reply(200,result());await pending;pending=p.load();reply(404,{});await pending;assert.equal(p.data.tracking,null);assert.deepEqual(p.data.events,[]);assert.ok(p.data.error);
 });
 console.log('PASS '+count+' tracking client groups');
})().catch(e=>{console.error(e);process.exitCode=1;});
