// Run frozen compiled client services. Only WeChat platform boundaries are simulated.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[2];
const fixture = JSON.parse(fs.readFileSync('/private/client-fixture.json', 'utf8'));
const base = 'http://application:8000';
const stored = new Map(), requests = [], downloads = [];
const dir = '/tmp/candidate-client'; fs.mkdirSync(dir, {recursive:true});
function allowed(url) { assert.equal(new URL(url).origin, base); return url; }
global.wx = {
  env: {USER_DATA_PATH:dir},
  getStorageSync:key=>stored.get(key),setStorageSync:(key,value)=>stored.set(key,value),removeStorageSync:key=>stored.delete(key),
  login:options=>options.success({code:'virtual-code-only'}),
  getFileSystemManager:()=>({accessSync:fs.accessSync,readdirSync:fs.readdirSync,unlinkSync:fs.unlinkSync,
    mkdirSync:(p,recursive)=>fs.mkdirSync(p,{recursive})}),
  request(options) {
    const url=allowed(options.url), endpoint=new URL(url).pathname;
    if(endpoint==='/api/miniprogram/v1/auth/wechat-session') {
      options.success({statusCode:200,data:{token:fixture.token,token_type:'Bearer',expires_in:900}});return;
    }
    requests.push(endpoint);
    fetch(url,{method:options.method,headers:{'Content-Type':'application/json',...options.header},
      body:options.data===undefined||options.method==='GET'?undefined:JSON.stringify(options.data)})
      .then(async r=>options.success({statusCode:r.status,data:await r.json()})).catch(e=>options.fail(e));
  },
  downloadFile(options) {
    downloads.push(new URL(allowed(options.url)).pathname);
    fetch(options.url,{headers:options.header}).then(async r=>{
      fs.writeFileSync(options.filePath,Buffer.from(await r.arrayBuffer()));options.success({statusCode:r.status});
    }).catch(e=>options.fail(e));
  }
};
require(path.join(root,'config.js')).getApiOrigin=()=>base;
require(path.join(root,'services/privacy.js')).assertPrivacyAuthorized=async()=>{};
const load=name=>require(path.join(root,'services',name+'.js'));
const session=load('member-session');
(async()=>{
  const identity=await session.loginMember();assert.equal(identity.member_public_id,fixture.member_public_id);
  assert.equal(await load('catalog-access').checkCatalogAccess(),'eligible');
  const catalog=load('catalog');assert.ok((await catalog.getCategories()).length);
  const listing=await catalog.getProducts('',1);assert.equal(listing.total,1);
  const product=await catalog.getProductDetail('PRD-ORDER-001');assert.equal(product.skus.length,2);
  assert.ok(product.main_image_url, 'Synthetic catalog fixture must publish a valid image URL');
  assert.ok(await load('catalog-images').downloadCatalogImage(product.main_image_url,product.product_public_id));
  const points=await session.getCurrentMemberPointsSummary();assert.equal(points.available_points,'170.00');
  const addresses=load('addresses');
  const address={recipient_name:'虚拟兼容会员',phone:'13800000001',province:'测试省',city:'测试市',district:'测试区',detail:'独立兼容联验地址'};
  await addresses.saveAddress('',address);
  const saved=(await addresses.listAddresses()).find(x=>x.detail===address.detail);assert.ok(saved);
  const lines=[{product_public_id:product.product_public_id,sku_code:product.skus[0].sku_code,quantity:1,points_price:product.skus[0].points_price}];
  const notice=await load('fulfillment').getFulfillment(lines);assert.equal(notice.ready,true);
  const orders=load('orders');orders.beginCheckout(identity.member_public_id,saved.address_public_id,address,lines,notice.digest);
  const placed=await orders.submitCheckout(identity.member_public_id);
  assert.equal(placed.status,'CREATED');
  assert.equal((await orders.submitCheckout(identity.member_public_id)).order_public_id,placed.order_public_id);
  assert.equal((await orders.resolveCheckout(identity.member_public_id)).order_public_id,placed.order_public_id);
  assert.equal((await orders.getOrder(placed.order_public_id)).status,'CREATED');
  assert.ok((await orders.listOrders(1)).total>=3);
  assert.equal((await orders.cancelOrder(placed.order_public_id)).status,'CANCELLED');
  assert.equal((await orders.cancelOrder(placed.order_public_id)).status,'CANCELLED');
  assert.equal((await session.getCurrentMemberPointsSummary()).available_points,'170.00');
  orders.clearPendingCheckout(identity.member_public_id);
  const current=fs.existsSync(path.join(root,'services/home-banners.js'));
  if(current) {
    const entries=await load('home-entries').getHomeEntries();assert.equal(entries.length,8);assert.ok(entries.every(x=>x.imageUrl));
    const banners=await load('home-banners').getHomeBanners();assert.deepEqual(banners.map(x=>x.slot),[2,1]);assert.ok(banners.every(x=>x.imageUrl));
  } else assert.ok(!requests.some(x=>/home-(entries|banners)/.test(x)));
  assert.ok(!JSON.stringify([...stored.values()]).includes(fixture.token));
  session.clearMemberSession();assert.equal(session.hasMemberSession(),false);
  assert.deepEqual(fs.readdirSync(dir+'/mall-private-images'),[]);
  console.log(JSON.stringify({client:current?'candidate':'legacy',real_http_requests:requests.length,private_downloads:downloads.length,
    catalog:true,points:true,addresses:true,order_idempotency:true,order_recovery_query:true,cancellation:true,logout_clears_private_images:true,
    token_not_persisted:true,new_home_media:current,wechat_login_boundary:'synthetic_preissued_token_only'}));
})().catch(e=>{session.clearMemberSession();console.error(e.stack);process.exitCode=1;});
