// Real compiled services/pages; virtual HTTP, wx identity, filesystem and clock only.
const assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(process.argv[2]);
const load=name=>require(path.join(root,name+'.js'));
let now=Date.now(), nextTimer=0;
const timers=new Map(), files=new Set(), storage=new Map(), requests=[], downloads=[], destinations=[];
Date.now=()=>now;
global.setTimeout=(fn,ms)=>{timers.set(++nextTimer,{fn,at:now+ms});return nextTimer;};
global.clearTimeout=id=>timers.delete(id);
global.setInterval=(fn,ms)=>{timers.set(++nextTimer,{fn,at:now+ms,ms});return nextTimer;};
global.clearInterval=global.clearTimeout;
const flush=async()=>{for(let i=0;i<120;i++)await Promise.resolve();};
async function advance(ms){now+=ms;for(const [id,t] of [...timers])if(t.at<=now){if(t.ms)t.at=now+t.ms;else timers.delete(id);await t.fn();}await flush();}
let owner='MEM-A',status='eligible',ttl=120,holdCatalog=false,holdDownload=false,downloadStatus=200,apiStatus=200,fsFailure=false,storageFailure=false;
const heldRequests=[];
const product={product_public_id:'PRD-A',name:'虚拟会员商品',subtitle:null,category_slug:'home',min_points_price:'10.00',in_stock:true,
 main_image_url:'/uploads/mall_products/PRD-A/main.webp',description:'测试说明',images:[{role:'main',url:'/uploads/mall_products/PRD-A/main.webp',alt_text:null}],
 skus:[{sku_code:'SKU-A',name:'测试规格',points_price:'10.00',in_stock:true,stock_status:'IN_STOCK'}]};
global.wx={env:{USER_DATA_PATH:'wxfile://usr'},getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>{if(storageFailure)throw Error('virtual storage error');storage.set(k,v);},
 getAccountInfoSync:()=>({miniProgram:{appId:'touristappid',envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'}),
 login:o=>o.success({code:'virtual-one-use-code'}),stopPullDownRefresh(){},
 navigateTo:o=>destinations.push(o.url),reLaunch:o=>destinations.push(o.url),navigateBack(){},
 getFileSystemManager:()=>({accessSync(){},mkdirSync(){},readdirSync:()=>[...files].filter(x=>x.startsWith('wxfile://usr/mall-private-images/')).map(x=>x.split('/').pop()),
 unlinkSync(p){if(fsFailure)throw Error('virtual unlink error');files.delete(p);}}),
 request(o){requests.push(o);const url=new URL(o.url),p=url.pathname;
  if(p.endsWith('/auth/wechat-session'))return o.success({statusCode:200,data:{token_type:'Bearer',token:'virtual.'+owner,expires_in:900}});
  assert.equal(o.header.Authorization,'Bearer virtual.'+owner);
  if(p.endsWith('/auth/me'))return o.success({statusCode:200,data:{member_public_id:owner}});
  if(p.endsWith('/catalog-access'))return o.success({statusCode:apiStatus,data:{status,valid_for_seconds:status==='eligible'?ttl:0}});
  if(holdCatalog && p.includes('/products')){heldRequests.push(o);return;}
  if(p.endsWith('/products'))return o.success({statusCode:apiStatus,data:{items:[structuredClone(product)],total:1,page:1,page_size:20}});
  if(p.endsWith('/products/PRD-A'))return o.success({statusCode:apiStatus,data:structuredClone(product)});
  if(p.endsWith('/categories'))return o.success({statusCode:apiStatus,data:[{name:'居家日用',slug:'home'}]});
  if(p.endsWith('/orders') && o.method==='POST' && status==='disabled')return o.success({statusCode:403,data:{}});
  if(p.endsWith('/orders'))return o.success({statusCode:200,data:{items:[],total:0,page:1,page_size:20}});
  if(p.endsWith('/rights-requests'))return o.success({statusCode:200,data:{items:[],total:0}});
  throw Error('Unexpected virtual path '+p);
 },
 downloadFile(o){assert.equal(o.header.Authorization,'Bearer virtual.'+owner);assert.ok(o.url.startsWith('http://127.0.0.1:8000/uploads/mall_products/'));
  assert.ok(!o.url.includes('virtual.')&&!o.url.includes('?'));downloads.push(o);
  if(!holdDownload){files.add(o.filePath);o.success({statusCode:downloadStatus,filePath:o.filePath});}return{abort(){}};
 }};
global.getCurrentPages=()=>[];let definition;
global.Page=d=>definition=d;
load('services/privacy').assertPrivacyAuthorized=async()=>{};
const session=load('services/member-session'), access=load('services/catalog-access'),state=load('services/catalog-state'),images=load('services/catalog-images'),catalog=load('services/catalog'),cart=load('services/cart');
const page=d=>({...d,data:structuredClone(d.data),setData(values){Object.assign(this.data,values);}});
const home=()=>page(load('services/catalog-page').catalogPage(false));
let passed=0;
async function login(){status='eligible';apiStatus=200;ttl=120;await session.loginMember();await access.checkCatalogAccess();}
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
 await test('anonymous entrance fetches no catalog or images and retains history/activation actions',async()=>{
  session.clearMemberSession();const p=home(),n=requests.length;p.onLoad();await p.onShow();
  assert.equal(p.data.accessState,'login');assert.equal(p.data.catalogReady,false);assert.equal(requests.length,n);
  p.onAccessOrders();p.onAccessRights();p.onAccessActivation();assert.deepEqual(destinations.slice(-3),['/pages/orders/index','/pages/rights/index','/pages/activation/index']);p.onUnload();
 });
 await test('eligible member loads real page cards using authenticated local images',async()=>{
  await login();const p=home();p.onLoad();await p.onShow();assert.equal(p.data.catalogReady,true);assert.equal(p.data.items[0].name,product.name);
  assert.ok(p.data.items[0].imageUrl.startsWith('wxfile://usr/mall-private-images/'));assert.ok(files.has(p.data.items[0].imageUrl));
  assert.equal([...storage.values()].some(x=>JSON.stringify(x).includes('virtual.MEM')),false);p.onUnload();
 });
 await test('original and thumbnail share safe authenticated transport; unsafe and other-product paths rejected',async()=>{
  const n=downloads.length;assert.equal(await images.downloadCatalogImage('https://evil.invalid/x.webp'),'');
  assert.equal(await images.downloadCatalogImage('/uploads/mall_products/PRD-A/../x.webp'),'');
  assert.equal(await images.downloadCatalogImage(product.main_image_url,'PRD-B'),'');assert.equal(downloads.length,n);
  const thumb=await images.downloadCatalogImage('/uploads/mall_products/PRD-A/main.thumb-640.webp','PRD-A');assert.ok(files.has(thumb));
 });
 await test('logout clears files, cart and visible product; old response cannot repopulate',async()=>{
  const p=home();await p.onShow();cart.addCartLine(product,product.skus[0]);assert.equal(cart.readCart().length,1);
  holdCatalog=true;const pending=p.refresh(true);await flush();session.clearMemberSession();heldRequests.shift().success({statusCode:200,data:{items:[product],total:1,page:1,page_size:20}});await pending;
  holdCatalog=false;assert.equal(p.data.items.length,0);assert.equal(files.size,0);assert.equal(cart.readCart().length,0);p.onUnload();
 });
 await test('account switch rejects a delayed old download and removes its late file',async()=>{
  await login();holdDownload=true;const late=images.downloadCatalogImage('/uploads/mall_products/PRD-A/late.webp');const outcome=late.catch(e=>e);
  const task=downloads.at(-1);owner='MEM-B';await login();files.add(task.filePath);task.success({statusCode:200});
  assert.ok(await outcome instanceof Error);assert.ok(!files.has(task.filePath));holdDownload=false;
 });
 await test('revocation poll scrubs catalog and cart but keeps member identity and own history requests',async()=>{
  await login();const p=home();await p.onShow();cart.addCartLine(product,product.skus[0]);status='disabled';await advance(30001);
  assert.equal(p.data.accessState,'disabled');assert.equal(p.data.items.length,0);assert.equal(cart.readCart().length,0);assert.equal(files.size,0);
  assert.ok(await session.getSignedInMember());await session.requestMemberPrivate('/orders?page=1','GET');await session.requestMemberPrivate('/rights-requests?page=1','GET');
  status='eligible';await p.onRetryAccess();assert.equal(p.data.items.length,1);p.onUnload();
 });
 await test('qualification deadline clears a visible detail without a new network call',async()=>{
  await login();ttl=2;load('pages/product-detail/index');const p=page(definition);p.onLoad({id:'PRD-A'});await p.onShow();assert.ok(p.data.product);
  const n=requests.length;await advance(2001);assert.equal(p.data.product,null);assert.equal(p.data.gallery.length,0);assert.equal(p.data.accessState,'expired');assert.equal(requests.length,n);p.onUnload();
 });
 await test('qualification faults close UI without logout; retry recovers',async()=>{
  await login();const p=home();await p.onShow();apiStatus=503;await advance(30001);assert.equal(p.data.catalogReady,false);assert.equal(p.data.accessState,'unavailable');assert.equal(p.data.items.length,0);assert.ok(session.hasMemberSession());
  apiStatus=200;await p.onRetryAccess();assert.equal(p.data.catalogReady,true);p.onUnload();
 });
 await test('403 image response closes shopping globally and deletes downloaded error body',async()=>{
  await login();const p=home();await p.onShow();downloadStatus=403;
  await assert.rejects(images.downloadCatalogImage('/uploads/mall_products/PRD-A/blocked.webp'));assert.equal(p.data.items.length,0);assert.equal(files.size,0);assert.ok(session.hasMemberSession());downloadStatus=200;p.onUnload();
 });
 await test('image outage leaves product and price readable; retry downloads just the failed image',async()=>{
  await login();downloadStatus=503;load('pages/product-detail/index');const p=page(definition);p.onLoad({id:'PRD-A'});await p.onShow();assert.ok(p.data.product);assert.equal(p.data.gallery[0].failed,true);
  p.onSkuTap({currentTarget:{dataset:{code:'SKU-A'}}});const key=p.data.gallery[0].key;downloadStatus=200;await p.onRetryImage({currentTarget:{dataset:{kind:'gallery',index:0,key}}});assert.equal(p.data.gallery[0].failed,false);assert.equal(p.data.selectedSkuCode,'SKU-A');p.onUnload();
 });
 await test('downloads are limited to four at once and queued old work sends no new requests',async()=>{
  await login();holdDownload=true;const start=downloads.length;
  const jobs=Array.from({length:12},(_,i)=>images.downloadCatalogImage('/uploads/mall_products/PRD-A/queue'+i+'.webp').catch(e=>e));assert.equal(downloads.length-start,4);
  session.clearMemberSession();for(const o of downloads.slice(start)){files.add(o.filePath);o.success({statusCode:200});}
  assert.equal((await Promise.all(jobs)).every(x=>x instanceof Error),true);assert.equal(downloads.length-start,4);assert.equal(files.size,0);holdDownload=false;
 });
 await test('hide and return rechecks qualification before cart display; expiry does not expose stale cart',async()=>{
  await login();cart.addCartLine(product,product.skus[0]);load('pages/cart/index');const p=page(definition);await p.onShow();assert.equal(p.data.items.length,1);p.onHide();assert.equal(p.data.items.length,0);
  status='expired';await p.onShow();assert.equal(p.data.catalogReady,false);assert.equal(p.data.items.length,0);p.onUnload();
 });
 await test('app background clears images, restart removes crash leftovers without touching other files',async()=>{
  await login();await images.downloadCatalogImage(product.main_image_url);let app;global.App=x=>app=x;load('app');app.onHide();assert.equal(files.size,0);
  files.add('wxfile://usr/mall-private-images/image-123-1.webp');files.add('wxfile://usr/unrelated.txt');app.onLaunch();assert.equal(files.has('wxfile://usr/mall-private-images/image-123-1.webp'),false);assert.ok(files.has('wxfile://usr/unrelated.txt'));files.delete('wxfile://usr/unrelated.txt');
 });
 await test('failed file cleanup is retried next launch, never reused as an image cache',async()=>{
  await login();const old=await images.downloadCatalogImage(product.main_image_url);fsFailure=true;session.clearMemberSession();assert.ok(files.has(old));fsFailure=false;images.clearCatalogImages();assert.equal(files.size,0);
 });
 await test('long browsing evicts older files while later images still download, at most eighty files',async()=>{
  await login();let last='';for(let i=0;i<100;i++)last=await images.downloadCatalogImage('/uploads/mall_products/PRD-A/browse'+i+'.webp');
  assert.ok(last);assert.ok(files.has(last));assert.ok(files.size<=80);
 });
 await test('failed cart deletion never exposes the old cart after changing accounts',async()=>{
  await login();cart.addCartLine(product,product.skus[0]);storageFailure=true;owner='MEM-C';await login();
  assert.throws(()=>cart.readCart(),/清理失败/);storageFailure=false;await login();assert.deepEqual(cart.readCart(),[]);
 });
 await test('new-checkout rejection clears products without removing historical order recovery marker',async()=>{
  await login();const p=home();await p.onShow();storage.set('mall_checkout_pending_v1:'+owner,{owner,request_id:'a'.repeat(32)});
  status='disabled';await assert.rejects(session.requestMemberPrivate('/orders','POST',{request_id:'a'.repeat(32)}));
  assert.equal(p.data.items.length,0);assert.ok(session.hasMemberSession());assert.equal(load('services/orders').pendingCheckout(owner),'a'.repeat(32));
  await session.requestMemberPrivate('/orders?page=1','GET');p.onUnload();
 });
 await test('expired session clears catalog and new login can recover without carrying cart',async()=>{
  await login();const p=home();await p.onShow();cart.addCartLine(product,product.skus[0]);await advance(870001);assert.equal(session.hasMemberSession(),false);assert.equal(p.data.items.length,0);assert.equal(cart.readCart().length,0);await login();await p.onRetryAccess();assert.equal(p.data.catalogReady,true);p.onUnload();
 });
 await test('checkout display expires while its own-order recovery remains available',async()=>{
  await login();ttl=2;storage.set('mall_checkout_pending_v1:'+owner,null);cart.addCartLine(product,product.skus[0]);
  load('services/addresses').listAddresses=async()=>[];load('services/fulfillment').getFulfillment=async()=>({ready:true,digest:'f'.repeat(64)});
  load('pages/checkout/index');const p=page(definition);await p.onShow();await flush();assert.equal(p.data.items.length,1);
  await advance(2001);assert.deepEqual(p.data.items,[]);assert.equal(p.data.fulfillment,null);assert.ok(session.hasMemberSession());p.onHide();
 });
 await test('401 catalog response clears session and images; status and auth are never query parameters',async()=>{
  await login();apiStatus=401;await assert.rejects(catalog.getProducts('',1));assert.equal(session.hasMemberSession(),false);assert.equal(files.size,0);
  assert.equal(requests.some(o=>o.url.includes('virtual.MEM')),false);
 });
 session.clearMemberSession();console.log(JSON.stringify({suite:'member-catalog-client',passed,native_wechat:false,real_database_access:false}));
})().catch(error=>{session.clearMemberSession();console.error(error);process.exitCode=1;});
