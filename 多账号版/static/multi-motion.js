/* Black-gold silk from the local 1.1 shader, with a bounded rendering lifecycle. */
(function () {
  'use strict';

  const vertexSource = `
    attribute vec2 position;
    varying vec2 vUv;
    void main() {
      vUv = position * 0.5 + 0.5;
      gl_Position = vec4(position, 0.0, 1.0);
    }
  `;
  const fragmentSource = `
    precision highp float;
    varying vec2 vUv;
    uniform float uTime;
    uniform vec2 uResolution;
    vec3 permute(vec3 x) { return mod(((x*34.0)+1.0)*x, 289.0); }
    float snoise(vec2 v){
      const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
      vec2 i  = floor(v + dot(v, C.yy));
      vec2 x0 = v -   i + dot(i, C.xx);
      vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
      vec4 x12 = x0.xyxy + C.xxzz;
      x12.xy -= i1;
      i = mod(i, 289.0);
      vec3 p = permute( permute( i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0 ));
      vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0);
      m = m*m; m = m*m;
      vec3 x = 2.0 * fract(p * C.www) - 1.0;
      vec3 h = abs(x) - 0.5;
      vec3 ox = floor(x + 0.5);
      vec3 a0 = x - ox;
      m *= 1.79284291400159 - 0.85373472095314 * ( a0*a0 + h*h );
      vec3 g;
      g.x  = a0.x  * x0.x  + h.x  * x0.y;
      g.yz = a0.yz * x12.xz + h.yz * x12.yw;
      return 130.0 * dot(m, g);
    }
    float fbm(vec2 p) {
      float v = 0.0;
      float a = 0.5;
      for(int i = 0; i < 3; i++) { v += a * snoise(p); p *= 2.0; a *= 0.5; }
      return v;
    }
    vec2 rotate(vec2 p, float a) {
      float c = cos(a), s = sin(a);
      return vec2(p.x * c - p.y * s, p.x * s + p.y * c);
    }
    void main() {
      vec2 uv = vUv;
      vec2 p = uv * 2.0;
      float aspect = uResolution.x / uResolution.y;
      p.x *= aspect;
      float t = uTime * 0.02;
      float wave1 = sin(p.x * 1.2 + p.y * 0.6 + t * 0.4) * 0.5;
      float wave2 = sin(p.x * 0.5 - p.y * 0.9 + t * 0.25 + 2.0) * 0.5;
      float wave3 = cos(p.x * 0.8 + p.y * 1.1 + t * 0.3 + 4.0) * 0.3;
      float waves = wave1 + wave2 + wave3;
      vec2 q = vec2(fbm(p * 0.7 + vec2(0.0, t) + waves * 0.12), fbm(p * 0.7 + vec2(5.2, 1.3 + t * 0.5) + waves * 0.1));
      float f = fbm(p * 0.8 + 1.6 * q + t * 0.2 + waves * 0.08);
      float flow = f * 0.55 + waves * 0.35 + q.x * 0.25;
      vec3 baseColor = vec3(0.008, 0.006, 0.004);
      vec3 goldDeepest = vec3(0.12, 0.08, 0.03);
      vec3 goldDeep = vec3(0.25, 0.17, 0.06);
      vec3 goldMid = vec3(0.45, 0.32, 0.10);
      vec3 gold = vec3(0.75, 0.58, 0.20);
      vec3 goldBright = vec3(0.90, 0.76, 0.40);
      vec3 goldLight = vec3(0.97, 0.89, 0.65);
      vec3 goldWhite = vec3(1.0, 0.96, 0.82);
      vec3 warmWhite = vec3(1.0, 0.98, 0.92);
      vec3 col = baseColor;
      col = mix(col, goldDeepest, smoothstep(-0.35, -0.05, flow));
      col = mix(col, goldDeep, smoothstep(-0.15, 0.15, flow));
      col = mix(col, goldMid, smoothstep(0.0, 0.3, flow));
      col = mix(col, gold, smoothstep(0.15, 0.4, flow));
      col = mix(col, goldBright, smoothstep(0.3, 0.5, flow));
      col = mix(col, goldLight, smoothstep(0.45, 0.65, flow));
      col = mix(col, goldWhite, smoothstep(0.6, 0.78, flow));
      col = mix(col, warmWhite, smoothstep(0.75, 0.9, flow));
      float e = 0.002;
      vec2 grad = vec2(fbm(p * 0.8 + vec2(e, 0.0) + q * 0.6 + vec2(t * 0.1, 0.0)) - fbm(p * 0.8 - vec2(e, 0.0) + q * 0.6 + vec2(t * 0.1, 0.0)), fbm(p * 0.8 + vec2(0.0, e) + q * 0.6 + vec2(0.0, t * 0.08)) - fbm(p * 0.8 - vec2(0.0, e) + q * 0.6 + vec2(0.0, t * 0.08))) / (2.0 * e);
      float silk1 = smoothstep(0.15, 0.65, 1.0 - length(grad - 0.25));
      col += silk1 * vec3(0.5, 0.4, 0.2) * 0.12;
      float silk2 = pow(smoothstep(0.35, 0.75, 1.0 - length(grad * 1.4 - 0.4 + sin(t * 0.5) * 0.1)), 2.0);
      col += silk2 * vec3(0.7, 0.65, 0.5) * 0.15;
      float silk3 = snoise(p * 5.0 + grad * 1.5 + t * 0.3) * 0.5 + 0.5;
      col += silk3 * 0.03 * vec3(0.95, 0.85, 0.55);
      for(int i = 0; i < 3; i++) {
        float fi = float(i);
        vec2 rp = rotate(p - vec2(0.0, 0.0), t * 0.03 + fi * 2.094);
        float band = sin(rp.x * 1.5 + rp.y * 0.8 + t * (0.3 + fi * 0.1) + fi * 3.0) * 0.5 + 0.5;
        band = pow(band, 8.0 + fi * 3.0);
        float bandMask = smoothstep(0.2, 0.6, flow + 0.2);
        col += band * bandMask * vec3(0.7, 0.6, 0.35) * (0.08 - fi * 0.015);
      }
      float arcHighlight = smoothstep(0.5, 0.82, sin(flow * 3.14 + t * 0.15) * 0.5 + 0.5);
      col += arcHighlight * vec3(0.85, 0.7, 0.38) * 0.15;
      vec2 gp = p * 3.0;
      float goldNoise = snoise(gp + vec2(t * 0.15, t * 0.1));
      float goldNoise2 = snoise(gp * 2.3 + vec2(-t * 0.12, t * 0.08));
      float particles = smoothstep(0.55, 0.85, goldNoise) * smoothstep(0.4, 0.7, goldNoise2);
      float twinkle = sin(uTime * 2.0 + goldNoise * 20.0) * 0.5 + 0.5;
      particles *= 0.5 + twinkle * 0.5;
      col += particles * vec3(0.7, 0.6, 0.4) * 0.18;
      float fineDust = snoise(p * 8.0 + vec2(t * 0.2, -t * 0.15)) * 0.5 + 0.5;
      fineDust = pow(fineDust, 6.0);
      col += fineDust * vec3(1.0, 0.85, 0.5) * 0.12;
      vec2 center = uv - 0.5;
      float vignette = 1.0 - dot(center, center) * 1.5;
      vignette = smoothstep(0.0, 0.75, vignette);
      col *= vignette;
      float spot = 1.0 - length(center) * 0.85;
      spot = smoothstep(0.0, 0.65, spot);
      col += spot * 0.06 * goldBright;
      float topGlow = smoothstep(0.0, 0.4, 1.0 - abs(uv.y - 0.15) * 3.0) * smoothstep(0.0, 0.5, uv.x) * smoothstep(0.0, 0.5, 1.0 - uv.x);
      col += topGlow * vec3(0.6, 0.45, 0.2) * 0.08;
      float grain = fract(sin(dot(uv * uResolution, vec2(12.9898, 78.233)) + uTime) * 43758.5453);
      col += (grain - 0.5) * 0.015;
      col = col / (col + vec3(0.5));
      col = pow(col, vec3(0.85));
      col *= vec3(1.02, 1.0, 0.96);
      gl_FragColor = vec4(col, 1.0);
    }
  `;

  let canvas = null;
  let gl = null;
  let program = null;
  let buffer = null;
  let vertexShader = null;
  let fragmentShader = null;
  let uniformTime = null;
  let uniformResolution = null;
  let enabled = true;
  let initialized = false;
  let disposed = false;
  let contextLost = false;
  let animationId = null;
  let resizeTimer = null;
  let frames = 0;
  let sceneTime = 0;
  let lastFrameTime = null;
  let renderBudgetMs = 0;
  let mobile = false;
  let targetFps = 120;
  const cleanups = [];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function listen(target, event, callback, options) {
    target.addEventListener(event, callback, options);
    cleanups.push(() => target.removeEventListener(event, callback, options));
  }

  function listenMedia(query, callback) {
    if (query.addEventListener) listen(query, 'change', callback);
    else {
      query.addListener(callback);
      cleanups.push(() => query.removeListener(callback));
    }
  }

  function compile(kind, source) {
    const shader = gl.createShader(kind);
    if (!shader) throw new Error('Shader unavailable');
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      throw new Error('Shader unavailable');
    }
    return shader;
  }

  function releaseRenderer() {
    if (gl && !contextLost) {
      if (buffer) gl.deleteBuffer(buffer);
      if (program) gl.deleteProgram(program);
      if (vertexShader) gl.deleteShader(vertexShader);
      if (fragmentShader) gl.deleteShader(fragmentShader);
    }
    buffer = program = vertexShader = fragmentShader = null;
    uniformTime = uniformResolution = null;
  }

  function createRenderer() {
    gl = null;
    try {
      gl = canvas.getContext('webgl', {
        alpha: false, antialias: false, depth: false, stencil: false,
        preserveDrawingBuffer: true, powerPreference: 'low-power'
      });
      if (!gl) return false;
      const precision = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
      const source = precision && precision.precision > 0
        ? fragmentSource : fragmentSource.replace('precision highp float;', 'precision mediump float;');
      vertexShader = compile(gl.VERTEX_SHADER, vertexSource);
      fragmentShader = compile(gl.FRAGMENT_SHADER, source);
      program = gl.createProgram();
      gl.attachShader(program, vertexShader);
      gl.attachShader(program, fragmentShader);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('Program unavailable');
      gl.useProgram(program);
      buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const position = gl.getAttribLocation(program, 'position');
      gl.enableVertexAttribArray(position);
      gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
      uniformTime = gl.getUniformLocation(program, 'uTime');
      uniformResolution = gl.getUniformLocation(program, 'uResolution');
      canvas.style.opacity = '1';
      return true;
    } catch (_) {
      releaseRenderer();
      gl = null;
      canvas.style.opacity = '0';
      // The CSS background is the fallback. A rendering feature must never block the app.
      return false;
    }
  }

  function sizeCanvas() {
    if (!canvas) return;
    const width = Math.max(1, Math.round(canvas.clientWidth || window.innerWidth));
    const height = Math.max(1, Math.round(canvas.clientHeight || window.innerHeight));
    mobile = window.matchMedia('(max-width: 768px)').matches;
    targetFps = mobile ? 18 : 120;
    const dpr = Math.min(window.devicePixelRatio || 1, mobile ? 1 : 1.5);
    const scale = mobile ? Math.min(dpr, 640 / width, 900 / height) : Math.min(dpr, 1920 / width, 1440 / height);
    const pixelWidth = Math.max(1, Math.round(width * scale));
    const pixelHeight = Math.max(1, Math.round(height * scale));
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    if (gl && !contextLost && program) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.useProgram(program);
      gl.uniform2f(uniformResolution, width, height);
    }
    updateDiagnostics();
  }

  function render() {
    if (!gl || !program || contextLost || disposed || document.hidden) return;
    gl.useProgram(program);
    gl.uniform1f(uniformTime, sceneTime);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    frames += 1;
    updateDiagnostics();
  }

  function canAnimate() {
    return initialized && !disposed && enabled && !!gl && !!program && !contextLost && !document.hidden && !reduceMotion.matches;
  }

  function updateDiagnostics() {
    if (!canvas || typeof canvas.setAttribute !== 'function') return;
    canvas.setAttribute('data-motion-running', String(animationId !== null));
    canvas.setAttribute('data-motion-frames', String(frames));
    canvas.setAttribute('data-motion-fps', String(canAnimate() ? targetFps : 0));
    canvas.setAttribute('data-motion-enabled', String(enabled));
    canvas.setAttribute('data-motion-reduced', String(reduceMotion.matches));
  }

  function stopLoop() {
    if (animationId !== null) cancelAnimationFrame(animationId);
    animationId = null;
    lastFrameTime = null;
    renderBudgetMs = 0;
    updateDiagnostics();
  }

  function tick(now) {
    animationId = null;
    if (!canAnimate()) return;
    if (lastFrameTime === null) {
      lastFrameTime = now;
      render();
    } else {
      const deltaMs = Math.max(0, Math.min(now - lastFrameTime, 200));
      lastFrameTime = now;
      // Shader time is elapsed time, independent of display refresh and draw count.
      sceneTime += deltaMs / 1000;
      renderBudgetMs += deltaMs;
      const intervalMs = 1000 / targetFps;
      if (renderBudgetMs + .001 >= intervalMs) {
        renderBudgetMs = Math.max(0, renderBudgetMs - intervalMs);
        // Never catch up with multiple draws in one browser animation frame.
        if (renderBudgetMs >= intervalMs) renderBudgetMs %= intervalMs;
        render();
      }
    }
    animationId = requestAnimationFrame(tick);
    updateDiagnostics();
  }

  function syncLoop() {
    if (!canAnimate()) {
      stopLoop();
      return;
    }
    if (animationId === null) animationId = requestAnimationFrame(tick);
    updateDiagnostics();
  }

  function handleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      if (disposed) return;
      sizeCanvas();
      render();
      syncLoop();
    }, 100);
  }

  function status() {
    updateDiagnostics();
    return {
      enabled, running: animationId !== null, fps: canAnimate() ? targetFps : 0,
      frames, webgl: !!gl && !!program && !contextLost, reducedMotion: reduceMotion.matches
    };
  }

  function init(element) {
    const nextCanvas = element || document.getElementById('fluid-canvas');
    if (!nextCanvas) return status();
    if (initialized && canvas === nextCanvas && !disposed) return status();
    if (initialized) dispose();
    canvas = nextCanvas;
    disposed = false;
    initialized = true;
    contextLost = false;
    frames = 0;
    sceneTime = 0;
    createRenderer();
    sizeCanvas();
    render();
    listen(window, 'resize', handleResize, { passive: true });
    listen(document, 'visibilitychange', syncLoop);
    listenMedia(reduceMotion, () => { render(); syncLoop(); });
    listen(canvas, 'webglcontextlost', event => {
      event.preventDefault();
      contextLost = true;
      canvas.style.opacity = '0';
      stopLoop();
    });
    listen(canvas, 'webglcontextrestored', () => {
      if (disposed) return;
      contextLost = false;
      releaseRenderer();
      createRenderer();
      sizeCanvas();
      render();
      syncLoop();
    });
    syncLoop();
    return status();
  }

  function setEnabled(value) {
    enabled = !!value;
    if (initialized && !disposed && !frames) render();
    syncLoop();
    return status();
  }

  function dispose() {
    disposed = true;
    stopLoop();
    clearTimeout(resizeTimer);
    resizeTimer = null;
    cleanups.splice(0).forEach(remove => remove());
    releaseRenderer();
    gl = null;
    canvas = null;
    initialized = false;
  }

  window.SparkMotion = Object.freeze({ init, setEnabled, dispose, status });
})();
