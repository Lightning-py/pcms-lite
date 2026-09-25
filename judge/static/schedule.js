"use strict";
// Use server-calculated delay rather than the browser's possibly incorrect clock.
const starts = Array.from(document.querySelectorAll("[data-start-delay]"))
  .map(element => Number(element.dataset.startDelay))
  .filter(delay => Number.isFinite(delay) && delay >= 0);
if (starts.length) {
  const deadline = performance.now() + Math.min(...starts) + 250;
  const tick = () => {
    const remaining = deadline - performance.now();
    if (remaining <= 0) {
      location.reload();
    } else {
      setTimeout(tick, Math.min(remaining, 2147483647));
    }
  };
  tick();
}
