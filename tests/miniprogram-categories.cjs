// Offline routing, race and pagination checks. Native rendering is separately accepted by the user.
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const root=path.resolve(process.argv[2]),source=path.resolve(process.argv[3]);let destinations=[],definition;
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'}),reLaunch:o=>destinations.push(['root',o.url]),navigateTo:o=>destinations.push(['child',o.url]),stopPullDownRefresh:()=>{}};
global.Page=x=>definition=x;
const catalog=require(path.join(root,'services/catalog.js'));
const {CATEGORY_ENTRIES,categoryOptions}=require(path.join(root,'services/category-navigation.js'));
const {catalogPage}=require(path.join(root,'services/catalog-page.js'));
const cats=CATEGORY_ENTRIES.map(x=>({name:x.name,slug:x.key==='food'?'snacks-live':x.key}));
const item=(n,slug='home')=>({product_public_id:'PRD-'+n,name:'商品'+n,subtitle:'说明',category_slug:slug,min_points_price:'10.00',in_stock:true,main_image_url:'/uploads/mall_products/PRD-'+n+'/main.png'});
const result=(items,total=items.length,page=1)=>({items,total,page,page_size:20});
const tap=key=>({currentTarget:{dataset:{key}}});
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const page=category=>{const def=catalogPage(category);return {...def,data:structuredClone(def.data),setData(x){Object.assign(this.data,x);}};};
let count=0,calls=[];
async function test(name,fn){destinations=[];calls=[];catalog.getCategories=async()=>cats;catalog.getProducts=async(slug,p,q)=>{calls.push([slug,p,q]);return result([item(p,slug)],1,p);};await fn();count++;console.log('PASS '+name);}
(async()=>{
 await test('exact eight names and ambiguous/unconfigured categories never become all-products',async()=>{
  assert.deepEqual(CATEGORY_ENTRIES.map(x=>x.name),['居家日用','滋味零食','食品生鲜','首饰箱包','数码家电','全球美妆','厨房用品','母婴儿童']);
  assert.equal(categoryOptions(cats).find(x=>x.key==='food').slug,'snacks-live');
  assert.equal(categoryOptions([{name:'滋味零食',slug:''}]).find(x=>x.key==='food').available,false);
  assert.equal(categoryOptions([...cats,{name:'滋味零食',slug:'other'}]).find(x=>x.key==='food').available,false);
 });
 await test('home renders two independent columns and links all eight entries',async()=>{
  catalog.getCategories=async()=>{throw Error('home must not depend on categories request');};catalog.getProducts=async()=>result([1,2,3,4,5].map(n=>item(n)));
  const p=page(false);p.onLoad();await flush();assert.deepEqual(p.data.columns.map(x=>x.items.length),[3,2]);
  for(const x of CATEGORY_ENTRIES)p.onCategoryTap(tap(x.key));
  assert.deepEqual(destinations,CATEGORY_ENTRIES.map(x=>['root','/pages/categories/index?category='+x.key]));
 });
 await test('deep link resolves catalog slug by name and selects matching label',async()=>{
  const p=page(true);p.onLoad({category:'food'});await flush();assert.equal(p.data.activeName,'滋味零食');assert.deepEqual(calls,[['snacks-live',1,'']]);assert.equal(p.data.items[0].category_slug,'snacks-live');
 });
 await test('unknown deep links safely open the aggregate, unknown taps do nothing',async()=>{
  const p=page(true);p.onLoad({category:'../invalid'});await flush();assert.equal(p.data.activeKey,'all');assert.deepEqual(calls,[['',1,'']]);p.onCategoryTap(tap('bad'));assert.equal(calls.length,1);
 });
 await test('unconfigured category produces empty state without querying all products',async()=>{
  catalog.getCategories=async()=>cats.filter(x=>x.name!=='食品生鲜');const p=page(true);p.onLoad({category:'fresh'});await flush();assert.equal(calls.length,0);assert.equal(p.data.loading,false);assert.deepEqual(p.data.items,[]);assert.equal(p.data.error,'');
 });
 await test('late product response cannot overwrite a newer category',async()=>{
  let pending=[];catalog.getProducts=(slug,p,q)=>new Promise(resolve=>pending.push({slug,resolve}));
  const p=page(true);p.onLoad({category:'home'});await flush();p.onCategoryTap(tap('food'));await flush();
  pending[1].resolve(result([item('NEW','snacks-live')]));await flush();pending[0].resolve(result([item('OLD','home')]));await flush();assert.equal(p.data.items[0].product_public_id,'PRD-NEW');
 });
 await test('switching while category metadata loads uses the latest request',async()=>{
  let pending=[];catalog.getCategories=()=>new Promise(resolve=>pending.push(resolve));const p=page(true);p.onLoad();p.onCategoryTap(tap('kids'));pending[1](cats);await flush();pending[0]([]);await flush();assert.equal(p.data.activeKey,'kids');assert.deepEqual(calls,[['kids',1,'']]);
 });
 await test('category search trims and retains its filter, clear restores it, overlong input rejected',async()=>{
  const p=page(true);p.onLoad({category:'food'});await flush();p.onSearchInput({detail:{value:'  果干  '}});p.onSearchSubmit();await flush();assert.deepEqual(calls[1],['snacks-live',1,'果干']);p.onClearSearch();await flush();assert.deepEqual(calls[2],['snacks-live',1,'']);p.onSearchInput({detail:{value:'x'.repeat(81)}});p.onSearchSubmit();assert.equal(calls.length,3);assert.ok(p.data.searchError);
 });
 await test('pagination keeps the category, retries failed pages and ignores duplicate taps',async()=>{
  let resolveMore;catalog.getProducts=async(slug,p,q)=>{calls.push([slug,p,q]);if(p===1)return result(Array.from({length:20},(_,i)=>item(i,slug)),21);return new Promise(resolve=>{resolveMore=resolve;});};
  const p=page(true);p.onLoad({category:'digital'});await flush();const next=p.onLoadMore();p.onLoadMore();assert.equal(calls.length,2);assert.deepEqual(calls[1],['digital',2,'']);resolveMore(result([item(20,'digital')],21,2));await next;assert.equal(p.data.items.length,21);assert.equal(p.data.total,21);assert.equal(p.data.hasMore,false);
  catalog.getProducts=async()=>{throw Error('offline');};p.data.hasMore=true;await p.onLoadMore();assert.equal(p.data.page,2);assert.equal(p.data.items.length,21);assert.ok(p.data.error);assert.equal(p.data.moreLoading,false);
 });
 await test('new category resets pagination and scroll position; old more response ignored',async()=>{
  let more;catalog.getProducts=async(slug,p)=>p===2?new Promise(resolve=>more=resolve):result(Array.from({length:20},(_,i)=>item(i,slug)),21);
  const p=page(true);p.onLoad({category:'home'});await flush();p.onProductsScroll({detail:{scrollTop:400}});const old=p.onLoadMore();p.onCategoryTap(tap('kids'));await flush();more(result([item('OLD')],21,2));await old;assert.equal(p.data.page,1);assert.equal(p.data.scrollTop,0);assert.ok(p.data.items.every(x=>x.category_slug==='kids'));
 });
 await test('metadata error is retryable; unloaded page ignores late requests',async()=>{
  catalog.getCategories=async()=>{throw Error('offline');};const p=page(true);p.onLoad();await flush();assert.ok(p.data.error);catalog.getCategories=async()=>cats;await p.refresh(true);assert.equal(p.data.error,'');
  let resolve;catalog.getProducts=()=>new Promise(r=>resolve=r);const pending=p.refresh(false);p.onUnload();resolve(result([item('LATE')]));await pending;assert.equal(p.data.items.length,0);
 });
 await test('failed images have fallback and detail routing rejects invalid IDs',async()=>{
  const p=page(false);await p.refresh(false);const x=p.data.items[0];p.onImageError({currentTarget:{dataset:{id:x.product_public_id,src:'stale'}}});assert.ok(p.data.items[0].imageUrl);p.onImageError({currentTarget:{dataset:{id:x.product_public_id,src:x.imageUrl}}});assert.equal(p.data.items[0].imageUrl,'');assert.equal(p.data.columns[0].items[0].imageUrl,'');p.onProductTap({currentTarget:{dataset:{id:'../bad'}}});assert.equal(destinations.length,0);p.onProductTap({currentTarget:{dataset:{id:x.product_public_id}}});assert.deepEqual(destinations,[['child','/pages/product-detail/index?id='+x.product_public_id]]);
 });
 await test('four dock destinations and points as a profile child',async()=>{
  const nav=fs.readFileSync(path.join(source,'templates/mall-nav.wxml'),'utf8');assert.deepEqual([...nav.matchAll(/bindtap="([^"]+)"/g)].map(x=>x[1]),['onCatalogTap','onCategoriesTap','onCartTap','onProfileTap']);assert.equal(nav.includes('我的积分'),false);
  for(const name of ['catalog','categories','cart','profile']){delete require.cache[require.resolve(path.join(root,'pages',name,'index.js'))];require(path.join(root,'pages',name,'index.js'));for(const fn of ['onCatalogTap','onCategoriesTap','onCartTap','onProfileTap'])if(fn!==({catalog:'onCatalogTap',categories:'onCategoriesTap',cart:'onCartTap',profile:'onProfileTap'})[name])assert.equal(typeof definition[fn],'function');}
  definition.onPointsTap();assert.deepEqual(destinations.pop(),['child','/pages/points/index']);assert.equal(fs.readFileSync(path.join(source,'pages/points/index.wxml'),'utf8').includes('mall-nav'),false);
 });
 console.log(`PASS ${count} category/navigation groups (offline)`);
})().catch(e=>{console.error(e);process.exitCode=1;});
