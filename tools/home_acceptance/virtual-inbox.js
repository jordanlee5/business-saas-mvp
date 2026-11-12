(() => {
 const box=document.createElement('p');box.id='virtual-inbox';box.style.cssText='padding:14px;background:#fff1cd;color:#614719;line-height:1.7';
 box.textContent='本地虚拟收件箱：获取验证码后将在这里显示，不会发到手机。';document.querySelector('#phone-login').prepend(box);
 const panel=document.querySelector('#phone-login');let stopped=false;
 window.addEventListener('pagehide',()=>{stopped=true;box.textContent='本地虚拟收件箱已清空';});
 async function poll(){
  if(stopped)return;
  try{const r=await fetch('/__acceptance/virtual/inbox',{cache:'no-store',headers:{'X-Phone-CSRF':panel.dataset.csrf}});
   const data=await r.json();if(r.ok)box.textContent=data.code?'本地虚拟验证码：'+data.code+'（仅本次验收，不是短信）':'本地虚拟收件箱：获取验证码后在此显示，不会发到手机。';
  }catch(_){} if(!stopped)setTimeout(poll,1200);
 }poll();
})();
