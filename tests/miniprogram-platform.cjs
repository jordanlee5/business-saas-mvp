// Offline negative checks. These never claim real WeChat authorization.
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(process.argv[2]);
let account = {miniProgram: {appId:'wx954783174df6dfdf', envVersion:'develop'}};
let calls = 0;
global.wx = {
  getAccountInfoSync: () => account,
  getSystemInfoSync: () => ({platform:'devtools'}),
  getPrivacySetting: () => { calls++; throw Error('must remain blocked'); },
  login: () => { calls++; throw Error('must remain blocked'); },
  request: () => { calls++; throw Error('must remain blocked'); },
  openPrivacyContract: () => { calls++; throw Error('must remain blocked'); },
};
const privacy = require(path.join(root, 'services/privacy.js'));
const session = require(path.join(root, 'services/member-session.js'));
const activation = require(path.join(root, 'services/activation.js'));
const config = require(path.join(root, 'config.js'));
let passed=0;
async function test(name, fn) {await fn(); assert.equal(calls,0); passed++; console.log('PASS '+name);}
(async()=>{
  await test('formal AppID does not enable privacy approval', async()=>{
    await assert.rejects(privacy.getPrivacySetting(), privacy.PrivacyAuthorizationError);
    await assert.rejects(privacy.openPrivacyContract(), privacy.PrivacyAuthorizationError);
  });
  await test('member login stays blocked before wx.login and requests', async()=>{
    await assert.rejects(session.loginMember(), privacy.PrivacyAuthorizationError);
  });
  await test('activation stays blocked before wx.login and requests', async()=>{
    await assert.rejects(activation.redeemActivation({businessPublicNo:'TEST',phone:'13800000000',plateNumber:'TEST',activationCode:'23456789AB'}),privacy.PrivacyAuthorizationError);
  });
  await test('tourist and unrelated AppIDs remain blocked', async()=>{
    for(const appId of ['touristappid','wx0000000000000000']) {
      account.miniProgram.appId=appId;
      await assert.rejects(privacy.getPrivacySetting(),privacy.PrivacyAuthorizationError);
    }
  });
  await test('only same-machine development simulator uses loopback',async()=>{
    assert.equal(config.resolveApiOrigin('develop','devtools'),'http://127.0.0.1:8000');
    for(const env of ['develop','trial','release']) for(const platform of ['ios','android'])
      assert.throws(()=>config.resolveApiOrigin(env,platform),config.ApiOriginConfigurationError);
    for(const env of ['trial','release'])
      assert.throws(()=>config.resolveApiOrigin(env,'devtools'),config.ApiOriginConfigurationError);
  });
  await test('configured HTTPS origin must meet project rules',async()=>{
    for(const origin of ['http://example.com','https://127.0.0.1','https://example.com:443','https://example.com/path','https://user:secret@example.com'])
      assert.throws(()=>config.resolveApiOrigin('release','ios',origin),config.ApiOriginConfigurationError);
    assert.equal(config.resolveApiOrigin('release','ios','https://example.com'),'https://example.com');
  });
  console.log(`PASS ${passed} platform configuration groups (offline)`);
})().catch(e=>{console.error(e);process.exitCode=1;});
