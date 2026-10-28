const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(process.argv[2] || path.join(__dirname, 'compiled'));
let pending = [], calls = [], pageDefinition;
global.wx = {
  getAccountInfoSync: () => ({ miniProgram: { envVersion: 'develop', appId: 'touristappid' } }),
  getSystemInfoSync: () => ({ platform: 'devtools' }),
  login: o => o.success({code:'test-code'}),
  request: o => { calls.push(o); pending.push(o); },
  setStorageSync: () => { throw Error('Sensitive storage forbidden'); },
  reLaunch: () => {},
};
global.Page = definition => { pageDefinition = definition; };
const privacy = require(path.join(root, 'services/privacy.js'));
// Only this test process replaces platform authorization, never production files.
privacy.assertPrivacyAuthorized = async () => {};
const session = require(path.join(root, 'services/member-session.js'));
const addresses = require(path.join(root, 'services/addresses.js'));
require(path.join(root, 'pages/addresses/index.js'));
const flush = async () => { for (let i=0;i<15;i++) await Promise.resolve(); };
const respond = (statusCode, data) => { assert.ok(pending.length); pending.shift().success({ statusCode, data }); };
const item = (n=1, is_default=true) => ({ address_public_id:'ADDR-'+n.toString(16).padStart(32,'0'), is_default,
  recipient_name:'测试收件人', phone:'+8613800000000', province:'测试省', city:'测试市', district:'测试区', detail:'测试地址1号' });
const list = items => ({items, total:items.length, limit:20});
async function login() {
  const promise = session.loginMember(); await flush();
  respond(200,{token_type:'Bearer',token:'test.signature',expires_in:900}); await flush();
  respond(200,{member_public_id:'MEM-TEST'}); await promise;
}
function page() {
  return {...pageDefinition, data:structuredClone(pageDefinition.data), generation:0,
    setData(values) { for (const [key,value] of Object.entries(values)) {
      if(key.startsWith('form.')) this.data.form[key.slice(5)] = value;
      else this.data[key] = value;
    }} };
}
let passed=0;
async function test(name, fn) { await fn(); assert.equal(pending.length,0); passed++; console.log('PASS '+name); }
(async () => {
  await test('guest sends no private requests', async()=>{
    session.clearMemberSession(); const p=page(); p.onShow(); await flush();
    assert.equal(p.data.hasSession,false); assert.equal(pending.length,0);
    await assert.rejects(addresses.listAddresses(), e=>e.status===401);
  });
  await login();
  await test('list uses bearer without personal URL data',async()=>{
    const promise=addresses.listAddresses(); const request=pending[0];
    assert.equal(request.header.Authorization,'Bearer test.signature');
    assert.equal(request.url,'http://127.0.0.1:8000/api/miniprogram/v1/addresses');
    respond(200,list([item()])); assert.deepEqual(await promise,[item()]);
  });
  await test('normalization and invalid fields rejected before request',async()=>{
    assert.equal(addresses.normalizeAddress({...item(),recipient_name:'  测试  '}).recipient_name,'测试');
    for(const input of [{...item(),phone:'１２３４５６７'}, {...item(),detail:'a\nsecret'}, {...item(),province:''}, {...item(),detail:'a\u200b'}, {...item(),detail:'a\n'}, {...item(),detail:'长'.repeat(241)}]) {
      await assert.rejects(addresses.saveAddress('',input),e=>e.status===422);
    }
  });
  await test('create sends only six fields and accepts 201',async()=>{
    const promise=addresses.saveAddress('',item()); assert.equal(pending[0].method,'POST');
    assert.deepEqual(Object.keys(pending[0].data).sort(),[...addresses.fields].sort());
    respond(201,item()); await promise;
  });
  await test('edit/default/delete contract',async()=>{
    let promise=addresses.saveAddress(item().address_public_id,item()); assert.equal(pending[0].method,'PUT'); respond(200,item()); await promise;
    promise=addresses.defaultAddress(item().address_public_id); assert.ok(pending[0].url.endsWith('/default')); assert.equal(pending[0].data,undefined); respond(200,item()); await promise;
    promise=addresses.removeAddress(item().address_public_id); assert.equal(pending[0].method,'DELETE'); respond(204,undefined); await promise;
  });
  await test('malformed IDs and lists fail closed',async()=>{
    await assert.rejects(addresses.removeAddress('../private'));
    for(const value of [list([item(),item()]),list([item(),item(2)]),{items:[],total:1,limit:20},list([{}])]) {
      const promise=addresses.listAddresses(); respond(200,value); await assert.rejects(promise);
    }
  });
  await test('HTTP errors never echo sensitive response',async()=>{
    for(const code of [404,409,422,503]) {
      const promise=addresses.listAddresses(); respond(code,{detail:'SECRET_PERSONAL_DATA'});
      await assert.rejects(promise,e=>e.status===code&&!e.message.includes('SECRET'));
    }
  });
  await test('logout invalidates in-flight response',async()=>{
    const promise=addresses.listAddresses(); session.clearMemberSession(); respond(200,list([item()]));
    await assert.rejects(promise,e=>e.status===401);
  });
  await login();
  await test('401 clears current session',async()=>{
    const promise=addresses.listAddresses(); respond(401,{}); await assert.rejects(promise,e=>e.status===401);
    assert.equal(session.hasMemberSession(),false);
  });
  await login();
  await test('old 401 cannot clear replacement login',async()=>{
    const old=addresses.listAddresses(); const request=pending.shift();
    await login(); request.success({statusCode:401,data:{}}); await assert.rejects(old,e=>e.status===401);
    assert.equal(session.hasMemberSession(),true);
  });
  await test('hide clears private data and ignores late list',async()=>{
    const p=page(); p.onShow(); p.onHide(); respond(200,list([item()])); await flush();
    assert.deepEqual(p.data.items,[]); assert.equal(p.data.form.phone,''); assert.equal(p.data.ready,false);
  });
  await test('20 entries block create but allow edit and default/delete',async()=>{
    const p=page(); p.onShow(); respond(200,list(Array.from({length:20},(_,i)=>item(i+1,i===0)))); await flush();
    p.onAddTap(); assert.equal(p.data.editing,false);
    p.onEditTap({currentTarget:{dataset:{id:item().address_public_id}}}); assert.equal(p.data.editing,true);
    p.onCancelTap(); p.onDeleteTap({currentTarget:{dataset:{id:item().address_public_id}}}); assert.equal(p.data.deleteDefault,true);
    p.onConfirmDeleteTap(); p.onConfirmDeleteTap(); assert.equal(pending.length,1);
    respond(204,undefined); await flush(); respond(200,list([item(2,false)])); await flush();
    assert.equal(p.data.items[0].is_default,false);
    p.onAddTap(); assert.equal(p.data.editing,true);
  });
  await test('duplicate save blocked; timeout requires list refresh',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    p.onSaveTap(); p.onSaveTap(); assert.equal(pending.length,1);
    pending.shift().fail({errMsg:'SECRET'}); await flush();
    assert.equal(p.data.ready,false); assert.equal(p.data.editing,false); assert.equal(p.data.form.phone,'');
    p.onSaveTap(); assert.equal(pending.length,0); p.onRetryTap(); respond(200,list([item()])); await flush();
    assert.equal(p.data.items.length,1);
  });
  await test('422 retains form for correction',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    p.onSaveTap(); respond(422,{}); await flush(); assert.equal(p.data.editing,true); assert.equal(p.data.busy,false);
  });
  await test('mutation success followed by refresh failure offers safe reload',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    p.onSaveTap(); respond(201,item()); await flush(); respond(503,{}); await flush();
    assert.equal(p.data.ready,false); assert.equal(p.data.editing,false); assert.equal(p.data.busy,false);
  });
  await test('return to page waits for in-flight write before reload',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    p.onSaveTap(); const write=pending.shift(); p.onHide(); p.onShow(); await flush();
    assert.equal(pending.length,0); assert.equal(p.data.loading,true);
    write.success({statusCode:201,data:item()}); await flush(); assert.equal(pending.length,1);
    respond(200,list([item()])); await flush(); assert.equal(p.data.items.length,1);
  });
  await test('malformed successful write response requires refresh',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    p.onSaveTap(); respond(201,{...item(),phone:'bad'}); await flush();
    assert.equal(p.data.ready,false); assert.equal(p.data.editing,false);
  });
  await test('expired session clears form before save',async()=>{
    const p=page(); p.onShow(); respond(200,list([])); await flush(); p.onAddTap(); p.data.form=addresses.normalizeAddress(item());
    session.clearMemberSession(); p.onSaveTap(); assert.equal(p.data.form.phone,''); assert.equal(p.data.hasSession,false);
  });
  console.log(`Client simulated checks: ${passed} PASS. Not platform acceptance.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
