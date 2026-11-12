const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const root=path.resolve(process.argv[2]),load=n=>require(path.join(root,'services',n+'.js'));
let destinations=[];
global.wx={getAccountInfoSync:()=>({miniProgram:{envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'}),reLaunch:o=>destinations.push(o.url)};
const session=load('member-session'),images=load('catalog-images'),service=load('home-banners');
const row=(slot,category='')=>({slot,title:'虚拟轮播'+slot,image_url:'/uploads/mall_banners/'+slot+'/'+'a'.repeat(32)+'.webp',target_category:category});
images.downloadHomeBannerImage=async(url,slot)=>'wxfile://'+slot;
images.forgetCatalogImage=()=>{};
load('catalog-guard').guardCatalogPage=d=>d;
load('home-entries').getHomeEntries=async()=>load('home-entries').defaultHomeEntries();
load('catalog').getProducts=async()=>({items:[],total:0,page:1,page_size:20});
const {catalogPage}=load('catalog-page');
const page=(isCategory=false)=>{const d=catalogPage(isCategory);return {...d,data:structuredClone(d.data),setData(x){Object.assign(this.data,x);}};};
const flush=async()=>{for(let i=0;i<80;i++)await Promise.resolve();};
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
 await test('server display order is preserved and empty list is valid',async()=>{
  session.requestMemberCatalog=async p=>{assert.equal(p,'/home-banners');return [row(3,'kids'),row(1)];};
  let data=await service.getHomeBanners();assert.deepEqual(data.map(x=>x.slot),[3,1]);assert.equal(data[0].targetCategory,'kids');
  session.requestMemberCatalog=async()=>[];assert.deepEqual(await service.getHomeBanners(),[]);
 });
 await test('duplicate slots, over capacity, bad labels and external destinations rejected',async()=>{
  for(const data of [null,[row(1),row(1)],Array.from({length:6},(_,i)=>row(i+1)),[{...row(1),title:''}],[row(1,'https://evil.invalid')],[{...row(1),slot:1.5}]]){
   session.requestMemberCatalog=async()=>data;await assert.rejects(service.getHomeBanners());
  }
 });
 await test('failed image is removed while other slides remain ordered',async()=>{
  session.requestMemberCatalog=async()=>[row(2),row(1)];images.downloadHomeBannerImage=async(p,s)=>s===2?'':'wxfile://1';
  assert.deepEqual((await service.getHomeBanners()).map(x=>x.slot),[1]);
  images.downloadHomeBannerImage=async()=>'';assert.deepEqual(await service.getHomeBanners(),[]);
 });
 const cards=()=>[1,2].map(slot=>({slot,title:'素材'+slot,imageUrl:'wxfile://'+slot,sourcePath:row(slot).image_url,targetCategory:slot===1?'food':''}));
 await test('slow artwork never awaits product refresh and older response cannot overwrite',async()=>{
  const pending=[];service.getHomeBanners=()=>new Promise(r=>pending.push(r));const p=page();
  await p.refresh(true);assert.equal(p.data.loading,false);await p.refresh(true);
  pending[1](cards());await flush();pending[0]([]);await flush();assert.equal(p.data.banners.length,2);
 });
 await test('logout clear or page unload rejects late banner response',async()=>{
  for(const close of ['onCatalogClear','onUnload']){
   let done;service.getHomeBanners=()=>new Promise(r=>done=r);const p=page();await p.refresh(true);p[close]();done(cards());await flush();assert.deepEqual(p.data.banners,[]);
  }
 });
 await test('old server and network failure return brand card without product error',async()=>{
  const p=page();p.data.banners=cards();service.getHomeBanners=async()=>{throw Error('404 old server');};
  await p.refresh(true);await flush();assert.deepEqual(p.data.banners,[]);assert.equal(p.data.error,'');
 });
 await test('pause resume index clamp and image errors stay scoped to current slide',async()=>{
  const p=page();p.data.banners=cards();p.onBannerPause();assert.equal(p.data.bannerPaused,true);p.onBannerPause();assert.equal(p.data.bannerPaused,false);
  p.onBannerChange({detail:{current:1}});assert.equal(p.data.bannerCurrent,1);p.onBannerChange({detail:{current:9}});assert.equal(p.data.bannerCurrent,1);
  p.onBannerImageError({currentTarget:{dataset:{slot:1,src:'stale'}}});assert.equal(p.data.banners.length,2);
  p.onBannerImageError({currentTarget:{dataset:{slot:1,src:'wxfile://1'}}});assert.equal(p.data.banners.length,1);assert.equal(p.data.bannerCurrent,0);
  p.onBannerImageError({currentTarget:{dataset:{slot:2,src:'wxfile://2'}}});assert.deepEqual(p.data.banners,[]);
 });
 await test('taps only allow configured internal category; display-only and forged slots ignored',async()=>{
  const p=page();p.data.banners=cards();for(const slot of [1,2,6])p.onBannerTap({currentTarget:{dataset:{slot}}});
  assert.deepEqual(destinations,['/pages/categories/index?category=food']);p.data.banners[0].targetCategory='https://evil.invalid';p.onBannerTap({currentTarget:{dataset:{slot:1}}});assert.equal(destinations.length,1);
 });
 await test('production template binds swiper to pause and retains existing green brand fallback',async()=>{
  const source=fs.readFileSync(path.join(process.argv[3],'pages/catalog/index.wxml'),'utf8');
  for(const text of ['<swiper ','banners.length > 1 && !bannerPaused','bindchange="onBannerChange"','binderror="onBannerImageError"','wx:else class="hero"','把每一份积分，'])assert.ok(source.includes(text));
 });
 console.log(JSON.stringify({suite:'home-banner-client',passed,native_wechat:false}));
})().catch(e=>{console.error(e);process.exitCode=1;});
