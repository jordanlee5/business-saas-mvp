(() => {
  'use strict';
  const carousel = document.querySelector('[data-carousel]');
  if (!carousel) return;
  const slides = Array.from(carousel.querySelectorAll('[data-slide]'));
  const controls = carousel.querySelector('.carousel-controls');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let current = 0, timer = null, hovered = false;
  function show(index) {
    current = (index + slides.length) % slides.length;
    slides.forEach((slide, n) => { slide.hidden = n !== current; });
    carousel.querySelector('[data-index]').textContent = `0${current + 1} / 0${slides.length}`;
  }
  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (slides.length > 1 && !reduced.matches && !hovered && !document.hidden && !carousel.contains(document.activeElement)) {
      timer = setTimeout(() => { show(current + 1); schedule(); }, 6500);
    }
  }
  controls.hidden = slides.length < 2;
  carousel.querySelector('[data-prev]').addEventListener('click', () => { show(current - 1); schedule(); });
  carousel.querySelector('[data-next]').addEventListener('click', () => { show(current + 1); schedule(); });
  carousel.addEventListener('mouseenter', () => { hovered = true; schedule(); });
  carousel.addEventListener('mouseleave', () => { hovered = false; schedule(); });
  carousel.addEventListener('focusin', schedule);
  carousel.addEventListener('focusout', () => setTimeout(schedule, 0));
  document.addEventListener('visibilitychange', schedule);
  reduced.addEventListener('change', schedule);
  carousel.querySelectorAll('img[data-fallback]').forEach(img => {
    function fallback() {
      if (img.dataset.fallback) { img.src = img.dataset.fallback; delete img.dataset.fallback; }
    }
    img.addEventListener('error', fallback);
    if (img.complete && !img.naturalWidth) fallback();
  });
  schedule();
})();
