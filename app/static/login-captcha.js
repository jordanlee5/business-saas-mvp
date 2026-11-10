(() => {
  const form = document.getElementById('login-form');
  const button = document.getElementById('login-button');
  const status = document.getElementById('captcha-status');
  let busy = false;
  const reset = message => {
    busy = false;
    button.disabled = false;
    status.textContent = message;
    document.getElementById('captcha-ticket').value = '';
    document.getElementById('captcha-randstr').value = '';
  };
  window.addEventListener('pageshow', () => reset('点击登录后，请先完成人机验证。'));
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    button.disabled = true;
    status.textContent = '请完成人机验证…';
    try {
      if (typeof window.TencentCaptcha !== 'function') throw new Error();
      const captcha = new window.TencentCaptcha(form.dataset.captchaAppId, result => {
        if (result.ret === 2) return reset('已取消验证，可重新点击登录。');
        if (result.ret !== 0 || !result.ticket || !result.randstr ||
            result.errorCode || result.ticket.startsWith('trerror')) {
          return reset('安全验证未完成，请稍后重新点击登录。');
        }
        document.getElementById('captcha-ticket').value = result.ticket;
        document.getElementById('captcha-randstr').value = result.randstr;
        status.textContent = '验证完成，正在登录…';
        HTMLFormElement.prototype.submit.call(form);
      }, {userLanguage: 'zh-cn'});
      captcha.show();
    } catch (_) {
      reset('安全验证加载失败，请检查网络后重新点击登录。');
    }
  });
})();
