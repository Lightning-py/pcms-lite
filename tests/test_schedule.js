// Run with node tests/test_schedule.js; no browser or external dependencies needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../judge/static/schedule.js'), 'utf8');
function page(delays) {
  let now = 0, timer = null, reloads = 0;
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => delays.map(startDelay => ({dataset: {startDelay}})) },
    performance: { now: () => now },
    location: { reload: () => { reloads++; } },
    setTimeout: (callback, delay) => { timer = {callback, delay}; }
  });
  return {
    timer: () => timer,
    reloads: () => reloads,
    advance: (elapsed) => { now += elapsed; const pending = timer; timer = null; pending.callback(); }
  };
}
const scheduled = page(['120000', '60000']);
assert.equal(scheduled.timer().delay, 60250);
scheduled.advance(60250);
assert.equal(scheduled.reloads(), 1);
assert.equal(page([]).timer(), null);
assert.equal(page(['NaN', '-1']).timer(), null);
const distant = page(['3000000000']);
assert.equal(distant.timer().delay, 2147483647);
distant.advance(2147483647);
assert.equal(distant.reloads(), 0);
assert.ok(distant.timer().delay < 2147483647);
console.log('Schedule refresh: start, absent schedule and long timeout — OK');
