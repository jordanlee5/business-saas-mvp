// Explicit LOCAL SIMULATION. No Tencent or SMS provider request is made.
window.TencentCaptcha=function(id,callback){this.show=async()=>{
  if(!window.confirm('这是本机虚拟人机验证，不调用真实服务。点击“确定”模拟通过，“取消”模拟放弃。')){callback({ret:2});return;}
  try{
    const response=await fetch('/__acceptance/virtual/proof',{method:'POST',headers:{'X-Phone-CSRF':document.querySelector('#phone-login').dataset.csrf}});
    if(!response.ok)throw Error();callback(await response.json());
  }catch(_){callback({ret:1});}
};};
