// End-to-end offline UI demo journey through compiled services and page logic.
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const root=path.resolve(process.argv[2]);let definition,storage=new Map(),navigated='';
global.Page=x=>definition=x;
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop',appId:'touristappid'}}),getSystemInfoSync:()=>({platform:'devtools'}),
 request:()=>{throw Error('Native network forbidden');},login:()=>{throw Error('Native login forbidden');},
 getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,v),navigateTo:x=>{navigated=x.url;},stopPullDownRefresh:()=>{}};
const session=require(path.join(root,'services/member-session.js')),orders=require(path.join(root,'services/orders.js')),
 tracking=require(path.join(root,'services/tracking.js')),demo=require(path.join(root,'demo-transport.js')),
 fulfillment=require(path.join(root,'services/fulfillment.js'));
function page(){return {...definition,data:structuredClone(definition.data),setData(x){Object.assign(this.data,x);}};}
(async()=>{
 const member=await session.loginMember();
 const addresses=await session.requestMemberPrivate('/addresses','GET'),address=addresses.items[0];
 const lines=[{product_public_id:'PRD-DEMO-001',sku_code:'SKU-DEMO-1',quantity:1,points_price:'120.00'},
 {product_public_id:'PRD-DEMO-003',sku_code:'SKU-DEMO-3',quantity:1,points_price:'180.00'}];
 async function place(){const notice=await fulfillment.getFulfillment(lines);orders.beginCheckout(member.member_public_id,address.address_public_id,address,lines,notice.digest);const order=await orders.submitCheckout(member.member_public_id);orders.clearPendingCheckout(member.member_public_id);return order;}
 const first=await place();demo.advance(first.order_public_id);demo.advance(first.order_public_id);demo.advance(first.order_public_id);
 const shipped=await orders.getOrder(first.order_public_id);assert.equal(shipped.parcels.length,2);
 require(path.join(root,'pages/orders/index.js'));const orderPage=page();await orderPage.show(first.order_public_id);
 orderPage.onTracking({currentTarget:{dataset:{id:shipped.parcels[1].parcel_id}}});assert.ok(navigated.endsWith('&parcel=2'));
 require(path.join(root,'pages/tracking/index.js'));const trackingPage=page();trackingPage.onLoad({order:first.order_public_id,parcel:'2'});await trackingPage.load();
 assert.equal(trackingPage.data.tracking.parcel_id,2);assert.ok(trackingPage.data.events[0].description.includes('包裹 2'));
 const normal=structuredClone(trackingPage.data);
 demo.setScenario('tracking_stale');await trackingPage.onPullDownRefresh();assert.equal(trackingPage.data.tracking.availability,'STALE');
 demo.setScenario('tracking_offline');await trackingPage.load();assert.equal(trackingPage.data.tracking.availability,'UNAVAILABLE');assert.deepEqual(trackingPage.data.events,[]);
 const second=await place();assert.equal(second.status,'CREATED');assert.notEqual(first.order_public_id,second.order_public_id);
 assert.equal((await orders.cancelOrder(second.order_public_id)).status,'CANCELLED');
 demo.setScenario('normal');await trackingPage.load();assert.equal(trackingPage.data.tracking.availability,'LIVE');
 trackingPage.onHide();assert.equal(trackingPage.data.tracking,null);
 const report={status:'PASS',scope:'offline_generated_miniprogram_services_and_pages',native_network_calls:0,parcels:2,
  checks:['order to selected parcel navigation','two parcel tracks','stale notice','query failure then successful checkout and cancellation','query recovery','hidden page privacy cleanup'],
  normal_view:normal,limitations:'Simulated WeChat I/O; not native WeChat rendering or a real carrier API'};
 if(process.argv[3])fs.writeFileSync(process.argv[3],JSON.stringify(report,null,2)+'\n');
 console.log('PASS offline parcel tracking journey, checkout/cancel during tracking failure');
})().catch(e=>{console.error(e);process.exitCode=1;});
