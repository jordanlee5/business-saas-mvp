// Node/offline checks only; no WeChat or real database operations.
const assert = require('node:assert/strict'), path = require('node:path');
const root = path.resolve(process.argv[2]), demoRoot = path.resolve(process.argv[3]);
const {beijingTimeText: format} = require(path.join(root, 'services/display-time.js'));
let count = 0;
async function test(name, run) { await run(); count++; console.log('PASS ' + name); }
(async () => {
  await test('equivalent timezone offsets display the same Beijing time', () => {
    for (const value of ['2026-10-29T14:38:12+08:00', '2026-10-29T06:38:12Z', '2026-10-29T02:38:12-04:00']) {
      assert.equal(format(value), '2026-10-29 14:38:12');
    }
  });
  await test('day, year and leap-day boundaries with fractional seconds', () => {
    assert.equal(format('2026-12-31T16:00:00.999Z'), '2027-01-01 00:00:00');
    assert.equal(format('2028-02-28T16:00:00Z'), '2028-02-29 00:00:00');
    assert.equal(format('2026-10-29T00:00:00+09:00'), '2026-10-28 23:00:00');
  });
  await test('ambiguous and invalid timestamps are not assigned a guessed timezone', () => {
    for (const value of ['', 'invalid', '2026-10-29', '2026-10-29T14:38:12', '2026-10-29T99:00:00Z']) {
      assert.equal(format(value), '时间暂不可用');
    }
  });
  await test('host timezone does not change presentation', () => {
    const previous = process.env.TZ;
    try {
      for (const zone of ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']) {
        process.env.TZ = zone;
        assert.equal(format('2026-12-31T16:00:00Z'), '2027-01-01 00:00:00');
      }
    } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
  });
  await test('generated demo grant, order and cancellation preserve the actual instant across midnight', () => {
    const realNow = Date.now;
    let instant = Date.parse('2026-12-31T15:59:59Z');
    Date.now = () => instant;
    try {
      const demo = require(path.join(demoRoot, 'demo-transport.js'));
      const request = (route, method='GET', data) => demo.dispatch({url:'/api/miniprogram/v1'+route, method, data});
      demo.resetDemo();
      assert.equal(request('/points/ledger').data.items[0].created_at, '2026-12-31T23:59:59+08:00');
      instant += 1000;
      const result = request('/orders', 'POST', {request_id:'test-time', expected_address:{}, lines:[{product_public_id:'PRD-DEMO-001',sku_code:'SKU-DEMO-1',quantity:1}]});
      assert.equal(result.statusCode, 201);
      const order = result.data;
      assert.equal(order.created_at, '2027-01-01T00:00:00+08:00');
      assert.equal(Date.parse(order.created_at), instant);
      assert.equal(request('/points/ledger').data.items[0].created_at, order.created_at);
      instant += 1000;
      request('/orders/'+order.order_public_id+'/cancel', 'POST');
      const refund = request('/points/ledger').data.items[0];
      assert.equal(refund.entry_type, 'RELEASE');
      assert.equal(refund.created_at, '2027-01-01T00:00:01+08:00');
    } finally { Date.now = realNow; }
  });
  await test('order list formats display only, preserves raw timestamp and clears it on hide', async () => {
    global.wx = {getAccountInfoSync:()=>({miniProgram:{appId:'touristappid',envVersion:'develop'}}),getSystemInfoSync:()=>({platform:'devtools'})};
    const session = require(path.join(root, 'services/member-session.js'));
    const orders = require(path.join(root, 'services/orders.js'));
    const source = {order_public_id:'ORD-0000000000000001',status:'CREATED',total_points:'120.00',total_quantity:1,created_at:'2026-12-31T16:00:00Z'};
    session.hasMemberSession = () => true;
    orders.listOrders = async () => ({items:[source], total:1,page:1,page_size:20});
    let definition; global.Page = value => definition = value;
    require(path.join(root, 'pages/orders/index.js'));
    const page = {...definition, data:structuredClone(definition.data), setData(value){Object.assign(this.data,value);}};
    await page.load(1);
    assert.equal(page.data.items[0].createdText, '2027-01-01 00:00:00');
    assert.equal(page.data.items[0].created_at, source.created_at);
    assert.equal('createdText' in source, false);
    page.onHide(); assert.deepEqual(page.data.items, []);
  });
  console.log(`PASS ${count} time presentation groups (offline)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
