// Offline page state and generated-demo checks; not native image/network acceptance.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]),demoRoot=path.resolve(process.argv[3]);
let definition,requests=[],stopped=0,added=0;
global.wx={getAccountInfoSync:()=>({miniProgram:{appId:'touristappid',envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'}),stopPullDownRefresh:()=>stopped++,request:()=>{throw Error('Native request forbidden');}};
global.Page=value=>definition=value;
const catalog=require(path.join(root,'services/catalog.js'));
catalog.getProductDetail=()=>new Promise((resolve,reject)=>requests.push({resolve,reject}));
require(path.join(root,'services/cart.js')).addCartLine=()=>added++;
require(path.join(root,'pages/product-detail/index.js'));
const formalDefinition=definition;
const product={product_public_id:'PRD-TEST',name:'测试商品',subtitle:null,category_slug:'home',min_points_price:'120.00',in_stock:true,main_image_url:'/uploads/mall_products/PRD-TEST/main.png',description:'介绍',
 images:['main','carousel','detail'].map((role,index)=>({role,url:`/uploads/mall_products/PRD-TEST/${index}.png`,alt_text:null})),
 skus:[{sku_code:'S1',name:'规格',points_price:'120.00',in_stock:true,stock_status:'IN_STOCK'}]};
function page(d=formalDefinition){return {...d,data:structuredClone(d.data),setData(value){Object.assign(this.data,value);}};}
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
async function load(p,data=product){p.data.id='PRD-TEST';const task=p.refresh();requests.shift().resolve(structuredClone(data));await task;}
function event(p,kind='gallery',index=0){return {currentTarget:{dataset:{kind,index,key:p.data[kind][index].key}}};}
let count=0;
async function test(name,run){await run();assert.equal(requests.length,0);count++;console.log('PASS '+name);}
(async()=>{
 await test('gallery failure keeps sibling images, chosen SKU and add-to-cart usable',async()=>{
  const p=page();await load(p);p.onSkuTap({currentTarget:{dataset:{code:'S1'}}});const e=event(p);p.onImageError(e);
  assert.equal(p.data.gallery[0].failed,true);assert.equal(p.data.gallery[1].failed,false);assert.equal(p.data.selectedSkuCode,'S1');
  p.onAddToCart();assert.equal(added,1);assert.equal(p.data.cartNotice,'已加入购物车');assert.equal(p.data.product.name,product.name);
 });
 await test('detail image retries once per action, stale error cannot override the new attempt',async()=>{
  const p=page();await load(p);const old=event(p,'detailImages');p.onImageError(old);assert.equal(p.data.detailImages[0].failed,true);
  p.onRetryImage(old);assert.equal(p.data.detailImages[0].failed,false);assert.notEqual(p.data.detailImages[0].key,old.currentTarget.dataset.key);
  const key=p.data.detailImages[0].key;p.onImageError(old);p.onRetryImage(old);assert.equal(p.data.detailImages[0].key,key);assert.equal(p.data.detailImages[0].failed,false);
  p.onImageError(event(p,'detailImages'));assert.equal(p.data.detailImages[0].failed,true);assert.equal(requests.length,0);
 });
 await test('refresh discards old image events and malformed targets',async()=>{
  const p=page();await load(p);const old=event(p);await load(p);p.onImageError(old);
  for(const dataset of [{kind:'gallery',index:-1,key:'bad'},{kind:'detailImages',index:99,key:'bad'},{kind:'product',index:0,key:'bad'}])p.onImageError({currentTarget:{dataset}});
  assert.equal(p.data.gallery[0].failed,false);
 });
 await test('no images or unsafe image paths yield the empty-image state',async()=>{
  const p=page();await load(p,{...product,images:[]});assert.equal(p.data.gallery.length,0);
  await load(p,{...product,images:[{role:'main',url:'https://other.example/image.png',alt_text:null}]});assert.equal(p.data.gallery.length,0);assert.ok(p.data.product);
 });
 await test('offline retry clears stale product, blocks repeat retry and restores chosen SKU using fresh data',async()=>{
  const p=page();await load(p);p.onSkuTap({currentTarget:{dataset:{code:'S1'}}});const failure=p.refresh();requests.shift().reject(Error('网络连接失败，请稍后重试'));await failure;
  assert.equal(p.data.product,null);assert.equal(p.data.selectedSku,null);assert.equal(p.data.gallery.length,0);assert.ok(p.data.error);
  const retry=p.onRetry();p.onRetry();assert.equal(requests.length,1);requests.shift().resolve({...product,skus:[{...product.skus[0],points_price:'140.00'}]});await retry;
  assert.equal(p.data.error,'');assert.equal(p.data.selectedSkuCode,'S1');assert.equal(p.data.selectedSku.points_price,'140.00');
 });
 await test('newer response wins and unloading another instance cannot cancel this page',async()=>{
  const p=page(),other=page();p.data.id='PRD-TEST';const first=p.refresh(),old=requests.shift(),second=p.refresh(),latest=requests.shift();
  other.onUnload();latest.resolve({...product,name:'新商品资料'});await second;old.resolve(product);await first;assert.equal(p.data.product.name,'新商品资料');
  const pending=p.refresh(),request=requests.shift();p.onUnload();request.resolve(product);await pending;assert.equal(p.data.product,null);
 });
 await test('pull-to-refresh ends after success and failure, removed SKU is not retained',async()=>{
  const p=page();await load(p);p.onSkuTap({currentTarget:{dataset:{code:'S1'}}});p.onPullDownRefresh();requests.shift().reject(Error('offline'));await flush();assert.equal(stopped,1);
  p.onPullDownRefresh();requests.shift().resolve({...product,skus:[{...product.skus[0],sku_code:'S2'}]});await flush();assert.equal(stopped,2);assert.equal(p.data.selectedSku,null);assert.equal(p.data.selectedSkuCode,'');
 });
 await test('not found is distinct from network error and subsequent reload can recover',async()=>{
  const p=page();const task=p.refresh();requests.shift().reject(new catalog.ProductNotFoundError('removed'));await task;assert.equal(p.data.notFound,true);assert.equal(p.data.error,'');
  await load(p);assert.equal(p.data.notFound,false);assert.ok(p.data.product);
 });
 const demo=require(path.join(demoRoot,'demo-transport.js'));
 require(path.join(demoRoot,'pages/product-detail/index.js'));const demoDefinition=definition;
 await test('generated demo detail network failure recovers through the real page retry action',async()=>{
  demo.setScenario('detail_offline_once');const p=page(demoDefinition);p.data.id='PRD-DEMO-001';await p.refresh();assert.ok(p.data.error);assert.equal(p.data.product,null);
  await p.onRetry();assert.equal(p.data.error,'');assert.equal(p.data.product.product_public_id,'PRD-DEMO-001');
 });
 await test('generated missing-image and long-copy fixtures reach detail and return to normal',async()=>{
  demo.setScenario('detail_images_missing');const p=page(demoDefinition);p.data.id='PRD-DEMO-001';await p.refresh();assert.ok(p.data.gallery[0].url.endsWith('/missing-main.png'));assert.ok(p.data.detailImages[0].url.endsWith('/missing-detail.png'));
  p.onImageError(event(p));p.onImageError(event(p,'detailImages'));assert.equal(p.data.gallery[0].failed,true);assert.equal(p.data.detailImages[0].failed,true);
  demo.setScenario('detail_long_text');await p.refresh();assert.ok(p.data.product.name.length>80);assert.ok(p.data.skus[0].name.length>100);
  demo.setScenario('normal');await p.refresh();assert.equal(p.data.gallery[0].failed,false);assert.ok(p.data.gallery[0].url.endsWith('/main.png'));
 });
 console.log(`PASS ${count} product detail groups (offline)`);
})().catch(error=>{console.error(error);process.exitCode=1;});
