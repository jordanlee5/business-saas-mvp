const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(process.argv[2]),base='http://127.0.0.1:'+JSON.parse(fs.readFileSync(path.join(root,'acceptance-manifest.json'),'utf8')).port,output=path.join(root,'data','browser-evidence');
fs.mkdirSync(output,{recursive:true});const checks=[],errors=[],external=[];
(async()=>{
 const browser=await chromium.launch({...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{channel:'msedge'}),headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});context.setDefaultTimeout(10000);
 await context.route('**/*',route=>{if(!route.request().url().startsWith(base+'/')){external.push(new URL(route.request().url()).origin);return route.abort();}return route.continue();});
 context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
 const page=await context.newPage();await page.goto(base+'/__acceptance');
 for(const width of [390,768,1440]){await page.setViewportSize({width,height:1000});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
 await page.screenshot({path:path.join(output,'portal.png'),fullPage:true});checks.push('portal at 390/768/1440 widths');
 const popup=context.waitForEvent('page');await page.getByRole('button',{name:'进入客户价格规则',exact:true}).click();const admin=await popup;
 await admin.waitForURL('**/mall-price-rules');
 async function price(percent){await admin.goto(base+'/mall-price-rules');await admin.locator('#price-org').selectOption({label:'虚拟总公司A（UAT-A）'});await admin.locator('#price-percent').fill(percent);await admin.getByRole('button',{name:'预览影响',exact:true}).click();try{await admin.locator('input[name=confirmed]').check();}catch(e){console.log('PREVIEW_FAILURE',admin.url(),await admin.textContent('body'));throw e;}await admin.getByRole('button',{name:'确认保存规则',exact:true}).click();await admin.waitForURL('**/mall-price-rules?*');assert((await admin.textContent('body')).includes('立即生效'));}
 await price('90');checks.push('real admin preview and immediate save from portal');
 const a1=await context.newPage();await a1.goto(base+'/__acceptance/member?profile=a1');await a1.locator('#price').filter({hasText:'90.00 积分'}).waitFor();
 assert((await a1.textContent('#balance')).includes('1000.00'));await a1.getByRole('button',{name:'准备兑换 1 件',exact:true}).click();await a1.locator('#quote').filter({hasText:'90.00'}).waitFor();
 await price('80');await a1.getByRole('button',{name:'确认兑换',exact:true}).click();await a1.locator('#message').filter({hasText:'机构或价格规则已变化'}).waitFor();checks.push('old prepared price refused after admin update');
 await a1.getByRole('button',{name:'刷新本会员'}).click();await a1.locator('#price').filter({hasText:'80.00 积分'}).waitFor();
 await a1.getByRole('button',{name:'准备兑换 1 件'}).click();await a1.locator('#quote').filter({hasText:'80.00'}).waitFor();
 await a1.getByRole('button',{name:'确认兑换',exact:true}).click();await a1.locator('#message').filter({hasText:'兑换成功'}).waitFor();
 assert((await a1.textContent('#balance')).includes('920.00'));await a1.getByRole('button',{name:'取消此单',exact:true}).click();await a1.locator('#message').filter({hasText:'订单已取消'}).waitFor();assert((await a1.textContent('#balance')).includes('1000.00'));
 checks.push('browser checkout and cancellation call real member endpoints');
 const a2=await context.newPage();await a2.goto(base+'/__acceptance/member?profile=a2');await a2.locator('#price').filter({hasText:'110.00 积分'}).waitFor();
 const b=await context.newPage();await b.goto(base+'/__acceptance/member?profile=b');await b.locator('#price').filter({hasText:'100.00 积分'}).waitFor();
 assert((await a1.textContent('#price')).includes('80.00'));checks.push('simultaneous tabs retain their own A1/A2/B identity');
 const limited=await context.newPage();await limited.goto(base+'/__acceptance/member?profile=limited');await limited.locator('#balance').filter({hasText:'70.00'}).waitFor();
 await limited.getByRole('button',{name:'准备兑换 1 件'}).click();await limited.locator('#quote').filter({hasText:'80.00'}).waitFor();await limited.getByRole('button',{name:'确认兑换',exact:true}).click();await limited.locator('#message.error').waitFor();
 assert((await limited.textContent('#balance')).includes('70.00'));checks.push('visible insufficient current-institution points refusal');
 const conflict=await context.newPage();await conflict.goto(base+'/__acceptance/member?profile=conflict');await conflict.locator('#price').filter({hasText:'多个机构'}).waitFor();assert(await conflict.locator('#prepare').isDisabled());checks.push('qualification conflict visibly blocks new purchase');
 for(const width of [390,768,1440]){await a1.setViewportSize({width,height:1000});assert(await a1.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
 await a1.screenshot({path:path.join(output,'member.png'),fullPage:true});
 assert.deepEqual(await a1.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);checks.push('member page responsive; no token or pending address persisted');
 assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
 fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({checks,passed:checks.length,page_errors:errors,external_requests:external,native_wechat:false},null,2));
 await browser.close();console.log(JSON.stringify({passed:checks.length,page_errors:errors,external_requests:external}));
})().catch(e=>{console.error(e);process.exit(1);});
