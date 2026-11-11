(() => {
  'use strict';
  const carousel = document.querySelector('[data-carousel]');
  if (!carousel) return;
  const slides = Array.from(carousel.querySelectorAll('[data-slide]'));
  const controls = carousel.querySelector('.carousel-controls');
  const pause = carousel.querySelector('[data-pause]');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let current = 0, timer = null, paused = reduced.matches, hovered = false;
  function show(index) {
    current = (index + slides.length) % slides.length;
    slides.forEach((slide, n) => { slide.hidden = n !== current; });
    carousel.querySelector('[data-index]').textContent = `0${current + 1} / 0${slides.length}`;
  }
  function schedule() {
    clearTimeout(timer);
    timer = null;
    pause.textContent = paused ? '播放轮播' : '暂停轮播';
    if (!paused && !hovered && !document.hidden && !carousel.contains(document.activeElement)) {
      timer = setTimeout(() => { show(current + 1); schedule(); }, 6500);
    }
  }
  controls.hidden = false;
  carousel.querySelector('[data-prev]').addEventListener('click', () => { show(current - 1); schedule(); });
  carousel.querySelector('[data-next]').addEventListener('click', () => { show(current + 1); schedule(); });
  pause.addEventListener('click', () => { paused = !paused; schedule(); });
  carousel.addEventListener('mouseenter', () => { hovered = true; schedule(); });
  carousel.addEventListener('mouseleave', () => { hovered = false; schedule(); });
  carousel.addEventListener('focusin', schedule);
  carousel.addEventListener('focusout', () => setTimeout(schedule, 0));
  document.addEventListener('visibilitychange', schedule);
  reduced.addEventListener('change', () => { paused = reduced.matches; schedule(); });
  schedule();
})();
