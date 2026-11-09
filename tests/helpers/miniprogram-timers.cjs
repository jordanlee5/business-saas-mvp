// Node-only runner helper: app expiry/poll timers must not keep finished unit checks alive.
// Short fixture/network callbacks remain referenced. Native WeChat does not import this file.
const timeout = global.setTimeout, interval = global.setInterval;
global.setTimeout = (fn, ms, ...args) => {
  const timer = timeout(fn, ms, ...args);
  if (ms >= 5000) timer.unref();
  return timer;
};
global.setInterval = (fn, ms, ...args) => {
  const timer = interval(fn, ms, ...args);
  timer.unref();
  return timer;
};
