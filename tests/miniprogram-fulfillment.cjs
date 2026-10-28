const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]);
global.wx={};
const {parseFulfillment}=require(path.join(root,'services/fulfillment.js'));
const {dispatch,setScenario,resetDemo,advance,clearDemoAddress,demoState}=require(path.join(process.argv[3],'demo-transport.js'));
const request=(url,method='GET',data={})=>dispatch({url:'/api/miniprogram/v1'+url,method,data});
const lines=[{product_public_id:'PRD-DEMO-001',sku_code:'SKU-DEMO-1',quantity:1,expected_points_price:'120.00'},
{product_public_id:'PRD-DEMO-003',sku_code:'SKU-DEMO-3',quantity:1,expected_points_price:'180.00'}];
let n=0;function test(name,fn){resetDemo();fn();n++;console.log('PASS '+name);}
test('two supplier notice validated without personal data',()=>{const x=request('/orders/fulfillment-preview','POST',{lines}).data;assert.equal(parseFulfillment(x).suppliers.length,2);assert.equal(JSON.stringify(x).includes('13800000000'),false);});
test('missing supplier is explicitly unready',()=>{setScenario('missing_supplier');const x=parseFulfillment(request('/orders/fulfillment-preview','POST',{lines}).data);assert.equal(x.ready,false);assert.equal(x.digest,'');});
test('malformed public profiles fail closed',()=>{const x=request('/orders/fulfillment-preview','POST',{lines}).data;for(const bad of [{...x,digest:'bad'},{...x,ready:true,suppliers:[]},{...x,suppliers:[{...x.suppliers[0],contact:123}]}])assert.throws(()=>parseFulfillment(bad));});
test('demo lost response resolves without second order',()=>{const body={request_id:'a'.repeat(32),lines,expected_address:{recipient_name:'DEMO'}};setScenario('timeout_after_submit');assert.throws(()=>request('/orders','POST',body));assert.equal(request('/orders/request/'+body.request_id).statusCode,200);request('/orders','POST',body);assert.equal(demoState().orders.length,1);});
test('demo fulfillment and cleared detail states available',()=>{const o=request('/orders','POST',{request_id:'a'.repeat(32),lines,expected_address:{recipient_name:'DEMO'}}).data;for(let i=0;i<3;i++)advance(o.order_public_id);assert.equal(request('/orders/'+o.order_public_id).data.status,'COMPLETED');clearDemoAddress(o.order_public_id);assert.equal(request('/orders/'+o.order_public_id).data.address_state,'CLEARED');});
test('demo has separate empty, offline and expired cases',()=>{setScenario('empty');assert.equal(request('/products').data.total,0);setScenario('offline');assert.throws(()=>request('/products'));setScenario('expired');assert.equal(request('/auth/me').statusCode,401);});
console.log(`PASS ${n} fulfillment/demo groups (offline, no platform acceptance)`);
