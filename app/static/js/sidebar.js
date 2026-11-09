/* Preserve only this tab's menu position; never scroll the document. */
(() => {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  const active = sidebar.querySelector('.side-link.active');
  if (active) active.setAttribute('aria-current', 'page');
  const key = 'saas.sidebar.scroll.v1';
  const save = () => { try { sessionStorage.setItem(key, String(sidebar.scrollTop)); } catch (_) {} };
  const reveal = () => {
    if (!active) return;
    const box = sidebar.getBoundingClientRect(), item = active.getBoundingClientRect();
    if (item.top < box.top + 12) sidebar.scrollTop += item.top - box.top - 12;
    else if (item.bottom > box.bottom - 12) sidebar.scrollTop += item.bottom - box.bottom + 12;
  };
  const restore = () => {
    try {
      const saved = sessionStorage.getItem(key);
      if (saved !== null && Number.isFinite(Number(saved))) sidebar.scrollTop = Number(saved);
    } catch (_) {}
    reveal();
  };
  restore();
  window.addEventListener('pageshow', restore);
  sidebar.addEventListener('scroll', save, {passive: true});
  sidebar.addEventListener('click', save, true);
  window.addEventListener('pagehide', save);
  window.addEventListener('resize', reveal);
})();
