// Actual compiled state/catalog/checkout services; only wx transport/storage is virtual.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]),store=new Map(),requests=[];
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop',appId:'touristappid'}}),getSystemInfoSync:()=>({platform:'devtools'}),
 getStorageSync:k=>store.get(k),setStorageSync:(k,v)=>store.set(k,v),request:o=>requests.push(o)};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
const state=require(path.join(root,'services/catalog-state.js'));
const orders=require(path.join(root,'services/orders.js'));
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
state.acceptPricingContext(a,'A1');state.allowCatalog(Date.now()+90000);
store.set('mall_cart_v1',[{product_public_id:'PRD-1',sku_code:'SKU-1',quantity:1}]);
const epoch=state.catalogEpoch();state.assertPricingContext(a);
state.acceptPricingContext(b,'A1');state.allowCatalog(Date.now()+90000);
assert.ok(state.catalogEpoch()>epoch);assert.equal(store.get('mall_cart_v1').length,1);
assert.throws(()=>state.assertPricingContext(a),/价格已变化/);
state.acceptPricingContext(b,'A1');state.allowCatalog(Date.now()+90000);
const address={recipient_name:'虚拟会员',phone:'13800000001',province:'测试省',city:'测试市',district:'测试区',detail:'测试地址'};
orders.beginCheckout('MEM-1','ADDR-'+'1'.repeat(32),address,[{product_public_id:'PRD-1',sku_code:'SKU-1',quantity:1,points_price:'36.00'}]);
assert.ok(orders.canRetryCheckout('MEM-1'));
state.acceptPricingContext(c,'A2');state.allowCatalog(Date.now()+90000);
assert.deepEqual(store.get('mall_cart_v1'),[]);assert.throws(()=>state.assertPricingContext(b));
assert.ok(orders.pendingCheckout('MEM-1'),'uncertain old order retained for safe lookup');
assert.ok(!JSON.stringify([...store]).includes(address.phone),'no recipient saved');
state.invalidateCatalog('login');assert.throws(()=>state.currentPricingContext());
console.log('PASS institution switch clears cart; same institution reprices; stale responses rejected; pending orders retained; no private storage');
