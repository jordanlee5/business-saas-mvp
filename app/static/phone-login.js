// No provider configuration, credentials, phone, OTP or challenge in persistent browser storage.
(() => {
  const panel = document.getElementById('phone-login');
  if (!panel || panel.dataset.enabled !== 'true') return;
  const phone = document.getElementById('phone-number'), code = document.getElementById('phone-code');
  const send = document.getElementById('phone-send'), submit = document.getElementById('phone-submit');
  const status = document.getElementById('phone-status');
  let busy = false, challenge = '', requestedPhone = '', deadline = 0, epoch = 0, controller;
  const update = () => {
    const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    send.textContent = remaining ? `${remaining}秒后可重新获取` : '获取验证码';
    send.disabled = busy || remaining > 0;
    submit.disabled = busy || !challenge;
    phone.disabled = busy; code.disabled = busy;
  };
  const clear = () => { epoch += 1; if (controller) controller.abort(); busy = false; challenge = ''; requestedPhone = ''; phone.value = ''; code.value = ''; status.textContent = ''; update(); };
  window.addEventListener('pagehide', clear);
  window.addEventListener('pageshow', clear);
  setInterval(update, 1000);
  phone.addEventListener('input', () => { challenge = ''; code.value = ''; update(); });
  const proof = () => new Promise((resolve, reject) => {
    let finished = false;
    const timer = setTimeout(() => done(null, '安全验证已超时，请重新尝试。'), 120000);
    const done = (value, error) => { if (finished) return; finished = true; clearTimeout(timer); error ? reject(new Error(error)) : resolve(value); };
    try {
      if (typeof window.TencentCaptcha !== 'function' || !panel.dataset.captchaAppId) throw new Error();
      new window.TencentCaptcha(panel.dataset.captchaAppId, result => {
        if (result.ret === 2) return done(null, '已取消安全验证。');
        if (result.ret !== 0 || !result.ticket || !result.randstr || result.errorCode || result.ticket.startsWith('trerror'))
          return done(null, '请重新完成人机验证。');
        done({captcha_ticket: result.ticket, captcha_randstr: result.randstr});
      }, {userLanguage: 'zh-cn'}).show();
    } catch (_) { done(null, '安全验证加载失败，请稍后重试。'); }
  });
  const request = async (path, body) => {
    const activeController = new AbortController();
    controller = activeController;
    const timer = setTimeout(() => activeController.abort(), 15000);
    try {
      const response = await fetch(path, {method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-Phone-CSRF': panel.dataset.csrf},
        body: JSON.stringify(body), signal: activeController.signal});
      const result = await response.json();
      if (response.status === 429) {
        const wait = Number(response.headers.get('Retry-After'));
        deadline = Date.now() + (Number.isFinite(wait) ? Math.max(1, Math.min(3600, wait)) : 60) * 1000;
      }
      if (!response.ok) throw new Error(result.detail || '暂时无法完成操作，请稍后重试。');
      return result;
    } finally { clearTimeout(timer); }
  };
  send.addEventListener('click', async () => {
    if (busy || deadline > Date.now()) return;
    const number = phone.value.trim();
    if (!/^1[3-9][0-9]{9}$/.test(number)) { status.textContent = '请输入11位有效手机号。'; return; }
    const current = ++epoch;
    busy = true; challenge = ''; code.value = ''; status.textContent = '请完成人机验证…'; update();
    try {
      const verified = await proof();
      if (current !== epoch) return;
      const result = await request('/login/phone/request-code', {phone: number, ...verified});
      if (current !== epoch) return;
      if (!/^[0-9a-f]{64}$/.test(result.challenge_id)) throw new Error('验证码申请未确认，请稍后重试。');
      challenge = result.challenge_id; requestedPhone = number; deadline = Date.now() + 60000;
      status.textContent = '如该号码已绑定可用管理员账号，将收到验证码。';
    } catch (error) {
      if (current === epoch) status.textContent = error.name === 'AbortError' ? '请求超时，请稍后重试。' : error.message;
    } finally { if (current === epoch) { busy = false; update(); } }
  });
  submit.addEventListener('click', async () => {
    if (busy || !challenge) return;
    if (phone.value.trim() !== requestedPhone || !/^[0-9]{6}$/.test(code.value)) { status.textContent = '请核对手机号并输入6位验证码。'; return; }
    const current = ++epoch; busy = true; update();
    try {
      const result = await request('/login/phone/verify', {challenge_id: challenge, code: code.value});
      if (current !== epoch) return;
      if (!['/dashboard','/change-password'].includes(result.redirect)) throw new Error('登录未确认，请刷新重试。');
      clear(); window.location.assign(result.redirect);
    } catch (error) {
      if (current === epoch) { code.value = ''; status.textContent = error.name === 'AbortError' ? '请求超时，请重新获取验证码。' : error.message; }
    } finally { if (current === epoch) { busy = false; update(); } }
  });
  update();
})();
