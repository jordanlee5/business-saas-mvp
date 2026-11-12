// Compiled production service/page, synthetic transport only.
const assert = require('node:assert/strict'), path = require('node:path');
const root = path.resolve(process.argv[2]), load = name => require(path.join(root, 'services', name + '.js'));
global.wx = {getAccountInfoSync:()=>({miniProgram:{envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'})};
const session = load('member-session'), images = load('catalog-images'), nav = load('category-navigation');
const manifest = () => nav.CATEGORY_ENTRIES.map(e => ({...e, image_url:null}));
const url = '/uploads/mall_home/home/' + 'a'.repeat(32) + '.webp';
let downloads=[];
images.downloadHomeEntryImage = async (image,key) => {downloads.push([image,key]);return image ? 'wxfile://'+key : '';};
const service = load('home-entries');
const flush = async()=>{for(let i=0;i<80;i++)await Promise.resolve();};
load('catalog-guard').guardCatalogPage = d=>d;
load('catalog').getProducts = async()=>({items:[],total:0,page:1,page_size:20});
const {catalogPage} = load('catalog-page');
const page=()=>{const d=catalogPage(false);return {...d,data:structuredClone(d.data),setData(x){Object.assign(this.data,x);}};};
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
 await test('eight fixed names and order survive reversed remote data; only images change',async()=>{
  const data=manifest().reverse();data.find(e=>e.key==='home').image_url=url;data.forEach(e=>e.name='cannot rename');
  session.requestMemberCatalog=async p=>{assert.equal(p,'/home-entries');return data;};
  const result=await service.getHomeEntries();
  assert.deepEqual(result.map(e=>[e.key,e.name]),nav.CATEGORY_ENTRIES.map(e=>[e.key,e.name]));
  assert.equal(result[0].imageUrl,'wxfile://home');assert.equal(result.filter(e=>e.imageUrl).length,1);
 });
 await test('malformed, duplicate and unknown slot sets are rejected before downloads',async()=>{
  for(const data of [null,[],[...manifest().slice(1),manifest()[1]],manifest().map(e=>({...e,image_url:42}))]){
   downloads=[];session.requestMemberCatalog=async()=>data;await assert.rejects(service.getHomeEntries());assert.equal(downloads.length,0);
  }
 });
 await test('slow artwork does not block products; old response cannot overwrite latest refresh',async()=>{
  let pending=[];service.getHomeEntries=()=>new Promise(r=>pending.push(r));const p=page();
  await p.refresh(true);assert.equal(p.data.loading,false);await p.refresh(true);
  const entries=service.defaultHomeEntries();entries[0].imageUrl='wxfile://new';pending[1](entries);await flush();
  pending[0](service.defaultHomeEntries());await flush();assert.equal(p.data.entries[0].imageUrl,'wxfile://new');
 });
 await test('cleared or unloaded page ignores delayed entry downloads',async()=>{
  for(const close of ['onCatalogClear','onUnload']){
   let done;service.getHomeEntries=()=>new Promise(r=>done=r);const p=page();await p.refresh(true);p[close]();
   const entries=service.defaultHomeEntries();entries[0].imageUrl='wxfile://late';done(entries);await flush();
   assert.ok(p.data.entries.every(e=>!e.imageUrl));
  }
 });
 await test('old server or failed configuration restores defaults and keeps product area usable',async()=>{
  const p=page();p.data.entries[0].imageUrl='wxfile://old';service.getHomeEntries=async()=>{throw Error('404 old server');};
  await p.refresh(true);await flush();assert.ok(p.data.entries.every(e=>!e.imageUrl));assert.equal(p.data.error,'');assert.equal(p.data.loading,false);
 });
 await test('image error falls back for matching image only and keeps category name',async()=>{
  const p=page();p.data.entries[0].imageUrl='wxfile://new';
  p.onEntryImageError({currentTarget:{dataset:{key:'home',src:'wxfile://old'}}});assert.ok(p.data.entries[0].imageUrl);
  p.onEntryImageError({currentTarget:{dataset:{key:'home',src:'wxfile://new'}}});assert.equal(p.data.entries[0].imageUrl,'');assert.equal(p.data.entries[0].name,'居家日用');
 });
 console.log(JSON.stringify({suite:'home-entry-client',passed,native_wechat:false}));
})().catch(e=>{console.error(e);process.exitCode=1;});
