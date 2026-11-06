// Thumbnail URL reaches the real catalog card and recovers after image failure.
const assert = require('node:assert/strict'), path = require('node:path');
const root = path.resolve(process.argv[2]);
require(path.join(root, 'config.js')).getApiOrigin = () => 'https://virtual.invalid';
const thumbnail = '/uploads/mall_products/PRD-THUMB/main_' + 'a'.repeat(32) + '.thumb-640.webp';
const legacy = '/uploads/mall_products/PRD-LEGACY/main.webp';
global.wx = {request(options) {
  assert.ok(options.url.startsWith('https://virtual.invalid/api/miniprogram/v1/products'));
  options.success({statusCode:200, data:{items:[thumbnail, legacy, null].map((image, index) => ({
    product_public_id:'PRD-' + index, name:'虚拟商品', subtitle:null, category_slug:'home',
    min_points_price:'10.00', in_stock:true, main_image_url:image,
  })), page:1, page_size:20, total:3}});
}};
(async () => {
  const definition = require(path.join(root, 'services/catalog-page.js')).catalogPage(false);
  const page = {...definition, data:structuredClone(definition.data), setData(values){Object.assign(this.data, values);}};
  await page.refresh(false);
  assert.equal(page.data.error, '');
  assert.deepEqual(page.data.items.map(item => item.imageUrl), ['https://virtual.invalid'+thumbnail, 'https://virtual.invalid'+legacy, '']);
  page.onImageError({currentTarget:{dataset:{id:'PRD-0',src:'https://virtual.invalid'+thumbnail}}});
  assert.equal(page.data.items[0].imageUrl, '');
  assert.equal(page.data.items[1].imageUrl, 'https://virtual.invalid'+legacy);
  await page.refresh(false);
  assert.equal(page.data.items[0].imageUrl, 'https://virtual.invalid'+thumbnail);
  console.log('PASS thumbnail/legacy/no-image catalog cards and image failure/recovery (mock HTTP, no native rendering)');
})().catch(error => { console.error(error); process.exitCode = 1; });
