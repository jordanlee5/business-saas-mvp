/* Only the generated local package loads this UI. Business results come from real HTTP routes. */
'use strict';
const $=id=>document.getElementById(id);
const profile=new URLSearchParams(location.search).get('profile')||'a1';
let token='',product=null,quote=null,pending=false;
const statusNames={CREATED:'待履约',FULFILLING:'履约中',SHIPPED:'已发货',COMPLETED:'已完成',CANCELLED:'已取消',REFUNDED:'已退款',PARTIALLY_SHIPPED:'部分发货'};
function message(value,error=false){$('message').textContent=value;$('message').className=error?'error':'';}
async function api(path,method='GET',body){
  const response=await fetch('/api/miniprogram/v1'+path,{method,cache:'no-store',headers:{Authorization:'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const value=await response.json();if(!response.ok)throw Error(value.detail||'暂时无法确认结果，请刷新检查订单');return value;
}
function text(tag,value){const e=document.createElement(tag);e.textContent=value;return e;}
function busy(value){pending=value;for(const button of document.querySelectorAll('button'))button.disabled=value;
  if(!value){$('prepare').disabled=!product;$('confirm').disabled=!quote;}}
async function orders(){
  const data=await api('/orders');$('orders').replaceChildren();
  if(!data.items.length)$('orders').append(text('p','暂无订单'));
  for(const order of data.items){
    const card=text('div','');card.className='order';
    card.append(text('strong',statusNames[order.status]||order.status),text('p',order.order_public_id),
      text('p',(order.organization?.name||'历史机构待核实')+' · 成交 '+order.total_points+' 积分'));
    const detail=text('button','查看订单详情');detail.onclick=()=>run(async()=>{
      const result=await api('/orders/'+order.order_public_id);
      const lines=result.items.map(row=>row.product_name+' × '+row.quantity+'；单价 '+row.unit_points_price+' 积分').join('；');
      message((result.organization?.name||'历史机构待核实')+' · '+result.total_points+' 积分；'+lines);
    });card.append(detail);
    if(order.status==='CREATED'){
      const cancel=text('button','取消此单');cancel.onclick=()=>run(async()=>{await api('/orders/'+order.order_public_id+'/cancel','POST');await refresh();message('订单已取消；可用积分已按原机构回退。');});card.append(cancel);
    }
    $('orders').append(card);
  }
}
async function refresh(){
  product=null;quote=null;$('quote').textContent='';$('price').textContent='—';
  const results=await Promise.allSettled([api('/points/summary'),api('/products/PRD-CLIENT-UAT'),api('/points/ledger'),orders()]);
  const [summary,detail,ledger,history]=results;
  if(summary.status==='fulfilled'){
    const value=summary.value;$('organization').textContent=value.organization?.name||'暂无机构';
    $('balance').textContent='可用 '+value.available_points+' 积分';$('reserved').textContent='预占 '+value.reserved_points+' 积分';
  }else{$('organization').textContent=summary.reason.message;$('balance').textContent='暂不可用';$('reserved').textContent='';}
  if(detail.status==='fulfilled'){product=detail.value;$('price').textContent=product.skus[0].points_price+' 积分';}
  else $('price').textContent=detail.reason.message;
  $('ledger').replaceChildren();
  if(ledger.status==='fulfilled'){
    const labels={GRANT:'发放',RESERVE:'订单预占',RELEASE:'取消退回',CONSUME:'履约扣除',REFUND:'退款',EXPIRE:'到期',ADJUST:'调整'};
    for(const row of ledger.value.items){const tr=document.createElement('tr');
      for(const value of [labels[row.entry_type]||row.entry_type,row.available_points_delta,row.reserved_points_delta,row.created_at])tr.append(text('td',value));$('ledger').append(tr);}
    $('ledger-note').textContent='本机构共 '+ledger.value.total+' 条记录（本页展示最新一页），不包含其他机构流水。';
  }else $('ledger-note').textContent=ledger.reason.message;
  if(history.status==='rejected')throw history.reason;
}
async function run(action){if(pending)return;busy(true);try{await action();}catch(error){message(error.message,true);}finally{busy(false);}}
$('refresh').onclick=()=>run(async()=>{await refresh();message('已读取最新价格、积分和订单。');});
$('prepare').onclick=()=>run(async()=>{
  if(!product)throw Error('请先刷新会员资料');
  const addresses=await api('/addresses'),address=addresses.items[0];if(!address)throw Error('缺少虚拟地址，请联系助手');
  const sku=product.skus[0];
  const lines=[{product_public_id:product.product_public_id,sku_code:sku.sku_code,quantity:1,expected_points_price:sku.points_price}];
  const notice=await api('/orders/fulfillment-preview','POST',{lines});if(!notice.ready)throw Error('虚拟受托发货资料未就绪');
  const {address_public_id,is_default,...expected_address}=address;
  quote={request_id:crypto.randomUUID().replaceAll('-',''),address_public_id,expected_address,lines,
    fulfillment_digest:notice.digest,pricing_context:product.pricing_context};
  $('quote').textContent='准备兑换 1 件，共 '+sku.points_price+' 积分。收件人：虚拟收件人；仅虚拟验收，不实际发货。';message('请核对后点击确认兑换。');
});
$('confirm').onclick=()=>run(async()=>{
  if(!quote)throw Error('请先准备兑换');
  // Retain this exact request on uncertain errors so retry cannot create another order.
  const order=await api('/orders','POST',quote);await refresh();message('兑换成功：'+order.order_public_id+'；成交 '+order.total_points+' 积分。');
});
run(async()=>{
  const response=await fetch('/__acceptance/member-session',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json',
    'x-acceptance-csrf':document.querySelector('meta[name="acceptance-csrf"]').content},body:JSON.stringify({profile})});
  const result=await response.json();if(!response.ok)throw Error(result.detail||'虚拟身份入口不可用');token=result.token;
  $('identity').textContent=result.label;await refresh();message('虚拟会员已连接。');
});
