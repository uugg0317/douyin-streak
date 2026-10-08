// Exercise the real background lifecycle with fake WebGL/RAF, without a browser.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync(path.resolve(__dirname, '../static/multi-motion.js'), 'utf8');

class FakeEventTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(listener);
  }
  removeEventListener(name, listener) { this.listeners.get(name)?.delete(listener); }
  dispatch(name, event = {}) {
    for (const listener of [...(this.listeners.get(name) || [])]) listener(event);
  }
  listenerCount() { return [...this.listeners.values()].reduce((n, set) => n + set.size, 0); }
}

function harness({width = 1280, height = 900, dpr = 2, webgl = true, reduced = false, shaderOK = true} = {}) {
  const frames = new Map(), timers = new Map(), errors = [], deleted = [], shaderTimes = [];
  let nextId = 1, drawCount = 0;
  class FakeNode extends FakeEventTarget {}
  class FakeElement extends FakeNode {
    constructor() {
      super(); this.attrs = new Map(); this.isConnected = true;
      this.style = {removeProperty(name) {delete this[name];}};
    }
    setAttribute(key, value) {this.attrs.set(key, String(value));}
    removeAttribute(key) {this.attrs.delete(key);}
    getAttribute(key) {return this.attrs.get(key) ?? null;}
    closest() {return null;}
    contains(node) {return node === this;}
    getBoundingClientRect() {return {left: 0, top: 0, width: 200, height: 200};}
  }
  const gl = {
    FRAGMENT_SHADER: 35632, VERTEX_SHADER: 35633, HIGH_FLOAT: 36338,
    COMPILE_STATUS: 35713, LINK_STATUS: 35714, ARRAY_BUFFER: 34962,
    STATIC_DRAW: 35044, FLOAT: 5126, TRIANGLE_STRIP: 5,
    createShader(kind) {return {kind};}, shaderSource() {}, compileShader() {},
    getShaderParameter() {return shaderOK;}, getShaderPrecisionFormat() {return {precision: 23};},
    createProgram() {return {};}, attachShader() {}, linkProgram() {},
    getProgramParameter() {return true;}, useProgram() {}, createBuffer() {return {};},
    bindBuffer() {}, bufferData() {}, getAttribLocation() {return 0;}, enableVertexAttribArray() {},
    vertexAttribPointer() {}, getUniformLocation(_, key) {return {key};},
    viewport() {}, uniform2f() {}, uniform1f(uniform, value) {if(uniform.key === 'uTime')shaderTimes.push(value);}, drawArrays() {drawCount++;},
    deleteBuffer() {deleted.push('buffer');}, deleteProgram() {deleted.push('program');},
    deleteShader() {deleted.push('shader');},
  };
  const canvas = new FakeElement();
  canvas.clientWidth = width; canvas.clientHeight = height;
  canvas.width = 1; canvas.height = 1;
  canvas.getContext = () => webgl ? gl : null;
  const reduce = new FakeEventTarget(); reduce.matches = reduced;
  const fine = new FakeEventTarget(); fine.matches = true;
  const mobile = new FakeEventTarget(); mobile.matches = width <= 768;
  const document = new FakeEventTarget(); document.hidden = false;
  document.getElementById = () => canvas;
  const window = new FakeEventTarget();
  window.innerWidth = width; window.innerHeight = height; window.devicePixelRatio = dpr;
  window.matchMedia = query => query.includes('prefers-reduced-motion') ? reduce : query.includes('max-width') ? mobile : fine;
  const sandbox = {
    window, document, Element: FakeElement, Node: FakeNode, Float32Array,
    requestAnimationFrame(fn) {const id = nextId++; frames.set(id, fn); return id;},
    cancelAnimationFrame(id) {frames.delete(id);},
    setTimeout(fn) {const id = nextId++; timers.set(id, fn); return id;},
    clearTimeout(id) {timers.delete(id);},
    console: {error(...args) {errors.push(args);}, warn() {}, log() {}},
  };
  vm.runInNewContext(source, sandbox, {filename: 'multi-motion.js'});
  return {
    motion: window.SparkMotion, window, document, canvas, reduce, fine, mobile, frames, timers, errors, deleted, shaderTimes,
    get drawCount() {return drawCount;},
    frame(now) {
      const callbacks = [...frames.values()]; frames.clear();
      callbacks.forEach(fn => fn(now));
    },
    resize(nextWidth, nextHeight) {
      canvas.clientWidth = window.innerWidth = nextWidth;
      canvas.clientHeight = window.innerHeight = nextHeight;
      mobile.matches = nextWidth <= 768;
      window.dispatch('resize');
      const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());
    },
    listenerCount() {return [window, document, canvas, reduce, fine].reduce((n, item) => n + item.listenerCount(), 0);},
    card() {
      const card = new FakeElement(); card.closest = selector => selector === '[data-tilt]' ? card : null; return card;
    },
  };
}

const results = [];
function scenario(name, run) {run(); results.push(name);}

scenario('Desktop animation targets 120 FPS and follows 60 Hz browser frames', () => {
  const h = harness(); h.motion.init(h.canvas);
  assert.equal(h.motion.status().fps, 120);
  assert.equal(h.frames.size, 1);
  const initial = h.drawCount;
  for(let i = 0; i < 60; i++)h.frame(100 + i * 1000 / 60);
  assert.equal(h.drawCount - initial, 60);
  assert.equal(h.frames.size, 1);
  assert(h.canvas.width <= 1920 && h.canvas.height <= 1440);
  h.motion.dispose();
});

scenario('A 120 Hz browser draws approximately 120 frames per second', () => {
  const h = harness(); h.motion.init(h.canvas); const initial = h.drawCount;
  for(let i = 0; i < 120; i++)h.frame(100 + i * 1000 / 120);
  assert.equal(h.drawCount - initial, 120);
  assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

scenario('A 144 Hz browser remains capped at 120 draws without halving the rate', () => {
  const h = harness(); h.motion.init(h.canvas); const initial = h.drawCount;
  for(let i = 0; i < 144; i++)h.frame(100 + i * 1000 / 144);
  assert(h.drawCount - initial >= 119 && h.drawCount - initial <= 120);
  assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

scenario('Background time advances by one second at 60, 120 and 144 Hz', () => {
  for(const hz of [60, 120, 144]) {
    const h = harness(); h.motion.init(h.canvas);
    for(let i = 0; i <= hz; i++)h.frame(100 + i * 1000 / hz);
    assert(Math.abs(h.shaderTimes.at(-1) - 1) < .01, `${hz} Hz must not change the flow speed`);
    h.motion.dispose();
  }
});

scenario('Pointer events never transform a panel or create another animation loop', () => {
  const h = harness(); h.motion.init(h.canvas); const card = h.card();
  for(const eventName of ['pointerenter', 'pointermove', 'pointerout', 'pointerleave', 'mousemove', 'mouseover', 'mouseout']) {
    h.document.dispatch(eventName, {target:card, relatedTarget:null, pointerType:'mouse', clientX:180, clientY:30});
    h.window.dispatch(eventName, {target:card, pointerType:'mouse', clientX:180, clientY:30});
  }
  assert.equal(card.style.transform, undefined);
  assert.equal(card.getAttribute('data-tilting'), null);
  assert.equal(h.frames.size, 1);
  assert.equal(h.document.listenerCount(), 1, 'Only the visibility lifecycle listener remains on document');
  h.motion.setEnabled(false); h.document.dispatch('pointermove', {target:card, pointerType:'mouse', clientX:180, clientY:30});
  assert.equal(card.style.transform, undefined); assert.equal(h.frames.size, 0);
  assert(!/rotateX|rotateY|tiltAnimationId|finePointer/.test(source));
  h.motion.dispose();
});

scenario('Mobile animation caps draw rate at 18 FPS and pixel ratio at one', () => {
  const h = harness({width: 390, height: 844, dpr: 3}); h.motion.init(h.canvas);
  assert.equal(h.motion.status().fps, 18);
  assert(h.canvas.width / 390 <= 1 && h.canvas.height / 844 <= 1);
  const initial = h.drawCount;
  h.frame(100); h.frame(116); h.frame(132); h.frame(148); h.frame(164);
  assert.equal(h.drawCount - initial, 2);
  h.motion.dispose();
});

scenario('Disabling animation cancels RAF and leaves no draw loop', () => {
  const h = harness(); h.motion.init(h.canvas); h.motion.setEnabled(false);
  const draws = h.drawCount;
  assert.equal(h.frames.size, 0); assert.equal(h.motion.status().running, false);
  assert.equal(h.motion.status().fps, 0); assert.equal(h.canvas.getAttribute('data-motion-enabled'), 'false');
  h.frame(300); assert.equal(h.drawCount, draws);
  h.motion.setEnabled(true); assert.equal(h.frames.size, 1);
  h.motion.setEnabled(true); assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

scenario('Hidden document pauses rendering and visible document resumes one loop', () => {
  const h = harness(); h.motion.init(h.canvas);
  h.document.hidden = true; h.document.dispatch('visibilitychange');
  const draws = h.drawCount;
  assert.equal(h.frames.size, 0); assert.equal(h.motion.status().running, false);
  h.frame(400); assert.equal(h.drawCount, draws);
  h.document.hidden = false; h.document.dispatch('visibilitychange');
  assert.equal(h.frames.size, 1); assert.equal(h.motion.status().running, true);
  h.document.dispatch('visibilitychange'); assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

scenario('Reduced-motion preference stops animation and can resume when changed', () => {
  const h = harness(); h.motion.init(h.canvas);
  h.reduce.matches = true; h.reduce.dispatch('change');
  assert.equal(h.frames.size, 0); assert.equal(h.motion.status().reducedMotion, true);
  assert.equal(h.motion.status().fps, 0); assert.equal(h.canvas.getAttribute('data-motion-reduced'), 'true');
  h.reduce.matches = false; h.reduce.dispatch('change');
  assert.equal(h.frames.size, 1); assert.equal(h.motion.status().fps, 120);
  h.motion.dispose();
  const initiallyReduced = harness({reduced: true}); initiallyReduced.motion.init(initiallyReduced.canvas);
  assert.equal(initiallyReduced.frames.size, 0); assert.equal(initiallyReduced.motion.status().fps, 0);
  initiallyReduced.motion.dispose();
});

scenario('Dispose removes listeners, resources, pending resize and RAF', () => {
  const h = harness(); h.motion.init(h.canvas);
  assert(h.listenerCount() > 0);
  h.window.dispatch('resize'); assert.equal(h.timers.size, 1);
  h.motion.dispose();
  assert.equal(h.frames.size, 0); assert.equal(h.timers.size, 0); assert.equal(h.listenerCount(), 0);
  assert.equal(h.motion.status().webgl, false); assert.equal(h.motion.status().running, false);
  assert.deepEqual(h.deleted.sort(), ['buffer', 'program', 'shader', 'shader'].sort());
  h.window.dispatch('resize'); h.document.dispatch('visibilitychange'); h.reduce.dispatch('change');
  assert.equal(h.frames.size, 0); assert.equal(h.timers.size, 0);
  h.motion.dispose(); assert.equal(h.listenerCount(), 0);
});

scenario('Unavailable WebGL or failed shader falls back without console errors', () => {
  for (const settings of [{webgl: false}, {shaderOK: false}]) {
    const h = harness(settings); h.motion.init(h.canvas);
    assert.equal(h.motion.status().webgl, false); assert.equal(h.frames.size, 0);
    assert.equal(h.motion.status().fps, 0); assert.equal(h.errors.length, 0);
    assert.equal(h.canvas.getAttribute('data-motion-running'), 'false');
    h.motion.dispose();
  }
});

scenario('Canvas exposes five diagnostic fields and increments frame counter', () => {
  const h = harness(); h.motion.init(h.canvas);
  const names = ['running', 'frames', 'fps', 'enabled', 'reduced'];
  names.forEach(name => assert.notEqual(h.canvas.getAttribute('data-motion-' + name), null));
  assert.equal(h.canvas.getAttribute('data-motion-running'), 'true');
  assert.equal(h.canvas.getAttribute('data-motion-fps'), '120');
  assert.equal(h.canvas.getAttribute('data-motion-enabled'), 'true');
  assert.equal(h.canvas.getAttribute('data-motion-reduced'), 'false');
  const initial = Number(h.canvas.getAttribute('data-motion-frames'));
  h.frame(100); assert(Number(h.canvas.getAttribute('data-motion-frames')) > initial);
  h.motion.dispose(); assert.equal(h.canvas.getAttribute('data-motion-running'), 'false');
});

scenario('Viewport changes recompute mobile and desktop budgets', () => {
  const h = harness(); h.motion.init(h.canvas);
  h.resize(390, 844); assert.equal(h.motion.status().fps, 18); assert(h.canvas.width <= 390);
  h.resize(1280, 900); assert.equal(h.motion.status().fps, 120); assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

scenario('Context loss pauses and restoration resumes without duplicate listeners', () => {
  const h = harness(); h.motion.init(h.canvas); const listeners = h.listenerCount();
  let prevented = false;
  h.canvas.dispatch('webglcontextlost', {preventDefault() {prevented = true;}});
  assert(prevented); assert.equal(h.frames.size, 0); assert.equal(h.motion.status().webgl, false);
  h.canvas.dispatch('webglcontextrestored');
  assert.equal(h.frames.size, 1); assert.equal(h.motion.status().webgl, true);
  assert.equal(h.listenerCount(), listeners);
  h.motion.init(h.canvas); assert.equal(h.listenerCount(), listeners); assert.equal(h.frames.size, 1);
  h.motion.dispose();
});

console.log(`Motion: ${results.length} isolated lifecycle scenarios passed`);
results.forEach(name => console.log('  PASS ' + name));
