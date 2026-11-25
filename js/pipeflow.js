/* 管道效果引擎：条件表达式求值 + 高亮 + 液体流动
 * 供 配置台(studio) 与 运行画面(runtime) 共用
 * 用法：
 *   const runner = new PipeRunner(scene, root);
 *   runner.build(pipesArray);          // 建链、造光点
 *   runner.apply(valuesObj);           // 每帧/每次数据更新调用
 *   runner.tick(dt);                   // 动画推进
 *   runner.setForce(id, true/false);   // 配置台「测试」用，忽略表达式
 */
import * as THREE from 'three';

const RX_ID = /^[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*(?:\[[^\]]*\])?/;

export function lex(s) {
  const t = []; let i = 0;
  s = String(s || '');
  while (i < s.length) {
    const c = s[i];
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r') { i++; continue; }
    const two = s.substr(i, 2);
    if (two === '>=' || two === '<=' || two === '==' || two === '!=' || two === '&&' || two === '||') {
      t.push({ k: 'op', v: two }); i += 2; continue;
    }
    if ('><()!+-*/'.includes(c)) { t.push({ k: 'op', v: c }); i++; continue; }
    let m = /^\d+(?:\.\d+)?/.exec(s.slice(i));
    if (m) { t.push({ k: 'num', v: parseFloat(m[0]) }); i += m[0].length; continue; }
    m = RX_ID.exec(s.slice(i));
    if (m) { t.push({ k: 'id', v: m[0] }); i += m[0].length; continue; }
    i++;
  }
  return t;
}

export function exprTags(expr) {
  if (!expr) return [];
  return lex(expr).filter(x => x.k === 'id').map(x => x.v);
}

/* 把 WinCC 来的值（可能是字符串 'true'/'false'）转成数 */
export function num(v) {
  if (v === undefined || v === null || v === '') return NaN;
  const s = String(v).toLowerCase().trim();
  if (s === 'true') return 1;
  if (s === 'false') return 0;
  return Number(s);
}

export function evalExpr(expr, values) {
  const t = lex(expr); let p = 0;
  const peek = () => t[p], eat = () => t[p++];
  const isOp = (v) => peek() && peek().k === 'op' && peek().v === v;

  function primary() {
    const x = eat();
    if (!x) return NaN;
    if (x.k === 'num') return x.v;
    if (x.k === 'id') {
      if (x.v === 'true') return 1;
      if (x.v === 'false') return 0;
      const r = values ? values[x.v] : undefined;
      if (r === undefined || r === null || r === '') return NaN;
      const b = String(r).toLowerCase();
      if (b === 'true') return 1;
      if (b === 'false') return 0;
      return Number(r);
    }
    if (x.v === '(') { const v = or(); if (isOp(')')) eat(); return v; }
    if (x.v === '-') return -unary();
    if (x.v === '+') return unary();
    if (x.v === '!') return unary() ? 0 : 1;
    return NaN;
  }
  function unary() { return peek() && isOp('-') ? (eat(), -unary()) : primary(); }
  function mul() {
    let v = unary();
    while (peek() && peek().k === 'op' && '*/'.includes(peek().v)) {
      const o = eat().v; const r = unary();
      v = o === '*' ? v * r : v / r;
    }
    return v;
  }
  function add() {
    let v = mul();
    while (peek() && peek().k === 'op' && '+-'.includes(peek().v)) {
      const o = eat().v; const r = mul();
      v = o === '+' ? v + r : v - r;
    }
    return v;
  }
  function cmp() {
    let v = add();
    while (peek() && peek().k === 'op' && ['>', '<', '>=', '<=', '==', '!='].includes(peek().v)) {
      const o = eat().v; const r = add();
      v = o === '>' ? (v > r ? 1 : 0) : o === '<' ? (v < r ? 1 : 0)
        : o === '>=' ? (v >= r ? 1 : 0) : o === '<=' ? (v <= r ? 1 : 0)
          : o === '==' ? (v === r ? 1 : 0) : (v !== r ? 1 : 0);
    }
    return v;
  }
  function and() {
    let v = cmp();
    while (isOp('&&')) { eat(); const r = cmp(); v = (v ? 1 : 0) && (r ? 1 : 0); }
    return v;
  }
  function or() {
    let v = and();
    while (isOp('||')) { eat(); const r = and(); v = (v ? 1 : 0) || (r ? 1 : 0); }
    return v;
  }
  if (!t.length) return NaN;
  try { return or(); } catch (e) { return NaN; }
}

function normName(s) { return String(s || '').replace(/[\s\[\]\.:\/\\]/g, ''); }

export class PipeRunner {
  constructor(scene, root) {
    this.scene = scene;
    this.root = root;
    this.states = [];
    this.force = {};
    this._idx = null;
  }

  _resolve(name) {
    if (!this.root) return null;
    if (!this._idx) {
      const m = new Map();
      this.root.traverse(o => {
        if (!o.name) return;
        if (!m.has(o.name)) m.set(o.name, o);
        const k = normName(o.name);
        if (!m.has(k)) m.set(k, o);
      });
      this._idx = m;
    }
    return this._idx.get(name) || this._idx.get(normName(name)) || null;
  }

  dispose() {
    this.states.forEach(st => {
      st.items.forEach(it => {
        if (!it.m) return;
        if (it.orig) it.m.material = it.orig;
        if (it.geoOrig) it.m.geometry = it.geoOrig;
      });
    });
    this.states.length = 0;
    this._idx = null;
  }

  /* 顶点 → 沿链条的弧长
     旧实现用「物体中心 + 主轴投影」：遇到 L 形/弯管时两条腿在主轴上的投影重叠，
     整段同时亮灭（看起来像一条色带横着扫过去），不会经过弯头。
     新实现：每个物体先拟合出自己的中心线折线（PCA 主轴分箱取质心），
     顶点按「在自己那条中心线上的最近点弧长」取 u；相邻物体的中心线首尾相接，
     弧长全局连续 ⇒ 流体沿真实走向扫过，包括弯头。 */

  _tagGeometry(mesh, poly) {
    const geo = mesh.geometry;
    if (!geo || !geo.attributes.position) return false;
    if (!mesh.userData._pfGeoOrig) mesh.userData._pfGeoOrig = geo;
    if (mesh.userData._pfGeo) return true;
    const g2 = geo.clone();
    const pos = g2.attributes.position;
    const arr = new Float32Array(pos.count);
    const mw = mesh.matrixWorld;
    const p = new THREE.Vector3();
    for (let i = 0; i < pos.count; i++) {
      p.set(pos.getX(i), pos.getY(i), pos.getZ(i)).applyMatrix4(mw);
      arr[i] = arcOfPoint(poly.pts, poly.cum, p);   // 最近点弧长（内部已夹取）
    }
    g2.setAttribute('aFlowU', new THREE.BufferAttribute(arr, 1));
    mesh.geometry = g2;
    mesh.userData._pfGeo = g2;
    return true;
  }

  _makeFlowMat(src, colorHex) {
    const m = src.clone();
    const uni = {
      uHead: { value: 0 }, uDir: { value: 1 }, uLen: { value: 1 }, uMin: { value: 0 },
      uTail: { value: 8 }, uCore: { value: 2 }, uCount: { value: 3 }, uOn: { value: 0 },
      uCol: { value: new THREE.Color(colorHex) }
    };
    m.onBeforeCompile = (sh) => {
      sh.uniforms.uHead = uni.uHead;
      sh.uniforms.uDir = uni.uDir;
      sh.uniforms.uLen = uni.uLen;
      sh.uniforms.uMin = uni.uMin;
      sh.uniforms.uTail = uni.uTail;
      sh.uniforms.uCore = uni.uCore;
      sh.uniforms.uCount = uni.uCount;
      sh.uniforms.uOn = uni.uOn;
      sh.uniforms.uCol = uni.uCol;
      sh.vertexShader = 'attribute float aFlowU;\nvarying float vFlowU;\n' +
        sh.vertexShader.replace('#include <begin_vertex>',
          '#include <begin_vertex>\n  vFlowU = aFlowU;');
      sh.fragmentShader =
        'varying float vFlowU;\nuniform float uHead, uDir, uLen, uMin, uTail, uCore, uCount, uOn;\nuniform vec3 uCol;\n' +
        sh.fragmentShader.replace('#include <emissivemap_fragment>',
          '#include <emissivemap_fragment>\n' +
          '  float _sum = 0.0;\n' +
          '  if (uOn > 0.5) {\n' +
          '    float _L = max(1.0, uLen);\n' +
          '    float _n = max(1.0, floor(uCount + 0.5));\n' +
          '    float _u = vFlowU - uMin;\n' +
          '    for (int k = 0; k < 32; k++) {\n' +
          '      if (float(k) >= _n) break;\n' +
          '      float _hk = mod(uHead + float(k) * (_L / _n), _L);\n' +
          '      float _d = (uDir > 0.0) ? (_hk - _u) : (_u - _hk);\n' +
          '      _d = mod(_d + _L, _L);\n' +
          '      float _core = max(0.02, uCore);\n' +
          '      float _tail = max(0.02, uTail);\n' +
          '      if (_d < _core) {\n' +
          '        _sum += 1.0;\n' +
          '      } else if (_d < _core + _tail) {\n' +
          '        _sum += exp(-((_d - _core) / _tail) * 3.4);\n' +
          '      }\n' +
          '    }\n' +
          '  }\n' +
          '  float _fo = pow(clamp(_sum, 0.0, 1.0), 0.75);\n' +
          '  float _hot = pow(_fo, 4.0);\n' +
          '  totalEmissiveRadiance += uCol * (_fo * 3.0 + 0.10 * uOn) + vec3(_hot * 0.7);\n' +
          '  diffuseColor.rgb = mix(diffuseColor.rgb, uCol, clamp(_fo * 1.0 + 0.12 * uOn, 0.0, 1.0));\n');
    };
    m.customProgramCacheKey = () => 'pipeflow-comet';
    m.needsUpdate = true;
    m.userData._pfU = uni;
    return m;
  }

  build(pipes) {
    this.dispose();
    this._idx = null;
    const list = pipes || [];

    list.forEach(p => {
      const objs = (p.objs || []).map(n => this._resolve(n)).filter(Boolean);
      if (!objs.length) return;
      const st = { rec: p, objs, on: null, items: [], chains: [], sp: 0 };

      /* 1) 每个物体拟合自己的中心线（世界坐标） */
      const polys = objs.map(o => objectPolyline(o));
      const use = [];
      objs.forEach((o, i) => { if (polys[i] && polys[i].length >= 2) use.push(i); });
      if (!use.length) return;

      /* 2) 按中心点距离分组（不相连的几段不强行串成一条），组内把中心线首尾相接 */
      const centers = use.map(i => new THREE.Box3().setFromObject(objs[i]).getCenter(new THREE.Vector3()));
      const groups = splitChains(centers);

      groups.forEach(gc => {
        const idxs = gc.map(c => use[centers.indexOf(c)]);
        const segs = idxs.map(i => polys[i]);
        const linked = linkPolylines(segs);

        /* 串成一条总路径：逐段追加，段间若有余隙补一段直连，保证弧长连续 */
        const P = [];
        const objRange = new Map();        // 局部序号 → {pts, cum}（cum 为全局弧长）
        let off = 0;
        linked.forEach(({ si, flip }) => {
          let q = segs[si];
          if (flip) q = q.slice().reverse();
          if (P.length) {
            const gap = P[P.length - 1].distanceTo(q[0]);
            off += gap;
            if (gap > 1e-4) { P.push(q[0].clone()); }
          }
          const u0 = off;
          const localCum = [0];
          for (let k = 1; k < q.length; k++) localCum.push(localCum[k - 1] + q[k].distanceTo(q[k - 1]));
          off += localCum[localCum.length - 1];
          const cum = localCum.map(v => v + u0);
          objRange.set(si, { pts: q, cum });
          for (let k = 0; k < q.length; k++) {
            if (!P.length || P[P.length - 1].distanceTo(q[k]) > 1e-6) P.push(q[k].clone());
          }
        });
        const total = Math.max(0.5, off);
        if (!(total > 0.2) || P.length < 2) return;

        const cum = [0];
        for (let k = 1; k < P.length; k++) cum.push(cum[k - 1] + P[k].distanceTo(P[k - 1]));

        const ch = { pts: P, cum, len: total, head: 0, uMin: 0, span: total };
        st.chains.push(ch);
        const ci = st.chains.length - 1;

        /* 3) 打弧长标记 + 换流动材质（每个物体只用自己的中心线取最近点） */
        const colorHex = p.color || '#19ffd8';
        idxs.forEach(si => {
          const o = objs[si];
          const poly = objRange.get(si);
          if (!poly) return;
          o.traverse(mm => {
            if (!mm.isMesh || !mm.material) return;
            this._tagGeometry(mm, poly);
            const mats = Array.isArray(mm.material) ? mm.material : [mm.material];
            const orig = mm.material;
            const news = mats.map(src => this._makeFlowMat(src, colorHex));
            news.forEach(u => { u.userData._pfU.uLen.value = ch.len; });
            mm.material = Array.isArray(mm.material) ? news : news[0];
            st.items.push({
              m: mm, orig, geoOrig: mm.userData._pfGeoOrig || mm.geometry,
              u: news.map(x => x.userData._pfU), chain: ci
            });
          });
        });
      });

      if (!st.chains.length) return;
      this.states.push(st);

      /* 4) 每条链按实际覆盖的 aFlowU 归一 */
      st.chains.forEach((ch, ci) => {
        let mn = Infinity, mx = -Infinity;
        st.items.filter(it => it.chain === ci).forEach(it => {
          const g = it.m.geometry, a = g && g.attributes && g.attributes.aFlowU;
          if (!a) return;
          for (let i = 0; i < a.count; i++) { const v = a.getX(i); if (v < mn) mn = v; if (v > mx) mx = v; }
        });
        if (!isFinite(mn) || mx - mn < 0.2) { ch.uMin = 0; ch.span = Math.max(0.5, ch.len); }
        else { ch.uMin = mn; ch.span = Math.max(0.5, mx - mn); }
        ch.head = 0;
      });
    });
    return this.states.length;
  }

  setForce(id, on) {
    if (on) this.force[id] = true; else delete this.force[id];
  }
  clearForce() { this.force = {}; }
  isForced(id) { return !!this.force[id]; }

  _setOn(st, on) {
    if (st.on === on) return false;
    st.on = on;
    const rec = st.rec;
    const mode = rec.mode || 'flow';
    const wantFlow = (mode === 'flow' || mode === 'both');
    const wantPaint = (mode === 'hl' || mode === 'both');
    const hex = rec.color || '#19ffd8';

    st.items.forEach(it => {
      const ch = st.chains[it.chain] || st.chains[0];
      const span = (ch && ch.span) || (ch && ch.len) || 1;
      it.u.forEach(u => {
        if (!u) return;
        u.uOn.value = (on && wantFlow) ? 1 : 0;
        u.uLen.value = span;
        u.uMin.value = (ch && ch.uMin) || 0;
        u.uHead.value = (ch && ch.head) || 0;
      });
      if (!wantPaint) return;
      const list = Array.isArray(it.m.material) ? it.m.material : [it.m.material];
      list.forEach(mm => {
        if (!mm.color) return;
        if (on) {
          if (mm.userData._pfC == null) mm.userData._pfC = mm.color.getHex();
          mm.color.set(hex);
          if (mm.emissive) {
            if (mm.userData._pfE == null) mm.userData._pfE = mm.emissive.getHex();
            mm.emissive.set(hex);
          }
          if ('emissiveIntensity' in mm) mm.emissiveIntensity = 0.5;
        } else {
          if (mm.userData._pfC != null) mm.color.setHex(mm.userData._pfC);
          if (mm.emissive && mm.userData._pfE != null) mm.emissive.setHex(mm.userData._pfE);
          if ('emissiveIntensity' in mm) mm.emissiveIntensity = 1;
        }
      });
    });
    return true;
  }

  apply(values) {
    this.values = values || {};
    let changed = false;
    this.states.forEach(st => {
      let on;
      if (this.force[st.rec.id]) on = true;
      else {
        const expr = (st.rec.expr || '').trim();
        if (!expr) on = true;
        else { const v = evalExpr(expr, this.values); on = isFinite(v) ? !!v : false; }
      }
      if (this._setOn(st, on)) changed = true;
    });
    return changed;
  }

  _motion(rec) {
    let sp = Number(rec.speed || 8);
    let dir = 1;
    const rv = rec.reverse;
    if (rv === 'on' || rv === true) dir = -1;
    const sv = (rec.speedVar || '').trim();
    if (sv) {
      const raw = num(this.values ? this.values[sv] : undefined);
      if (isFinite(raw)) {
        const full = Number(rec.speedFull || 100) || 100;
        let k = Math.abs(raw) / full;
        if (!isFinite(k) || k < 0) k = 0;
        if (k > 3) k = 3;
        if (Math.abs(raw) < 1e-9) k = 0;
        sp = sp * k;
        if (rv === 'sign') dir = raw < 0 ? -1 : 1;
      }
    }
    return { sp: sp, dir: dir };
  }

  tick(dt) {
    let any = false;
    this.states.forEach(st => {
      const rec = st.rec;
      const mo = this._motion(rec);
      const tau = Math.max(0.05, Number(rec.ramp || 0.7));
      const a = 1 - Math.exp(-dt / tau);
      st.sp += (mo.sp - st.sp) * a;
      if (Math.abs(st.sp) < 0.02) st.sp = 0;
      if (!st.on) return;
      if (!(st.sp > 0)) return;
      any = true;
      const dir = mo.dir;
      const tail = Math.max(0.2, Number(rec.tail || 6));
      const core = Math.max(0.2, Number(rec.core || 2));
      const count = Math.max(1, Math.min(32, Math.round(Number(rec.count || 3))));
      st.chains.forEach(ch => {
        const span = ch.span || ch.len;
        ch.head = (ch.head + dir * st.sp * dt) % span;
        if (ch.head < 0) ch.head += span;
      });
      st.items.forEach(it => {
        const ch = st.chains[it.chain] || st.chains[0];
        const span = ch.span || ch.len;
        it.u.forEach(u => {
          if (!u) return;
          u.uHead.value = ch.head;
          u.uDir.value = dir;
          u.uLen.value = span;
          u.uMin.value = ch.uMin || 0;
          u.uTail.value = tail;
          u.uCore.value = core;
          u.uCount.value = count;
        });
      });
    });
    return any;
  }
}

/* ---------- 折线按弧长取点 / 求弧长 ---------- */
/* 链条在弧长 u 处的切向；若与物体主轴近乎垂直（链条在此拐弯，切向不可信），
   改用物体自身的主轴向，保证竖直管 / 长直管都能拿到正确的方向 */
function chainTangent(pts, cum, u, center, bbox) {
  const e = 0.08;
  const a = pointAtArc(pts, cum, Math.max(0, u - e));
  const b = pointAtArc(pts, cum, Math.min(cum[cum.length - 1], u + e));
  const T = new THREE.Vector3().subVectors(b, a);
  if (T.lengthSq() < 1e-9) T.set(0, 1, 0);
  T.normalize();

  // 物体主轴（世界轴近似：取包围盒最长的那个方向）
  const s = new THREE.Vector3();
  bbox.getSize(s);
  const arr = [s.x, s.y, s.z].slice().sort((a, b) => b - a);
  const elongated = arr[0] > arr[1] * 2.0;      // 明显细长（普通直管）
  if (!elongated) return T;                     // 弯管 / 方盒：跟链条切向走

  const ax = new THREE.Vector3();
  if (s.x === arr[0]) ax.set(1, 0, 0);
  else if (s.y === arr[0]) ax.set(0, 1, 0);
  else ax.set(0, 0, 1);
  if (ax.dot(T) < 0) ax.negate();               // 与流向同侧
  return ax;
}
function pointAtArc(pts, cum, s) {
  const L = cum[cum.length - 1];
  s = Math.max(0, Math.min(L, s));
  for (let i = 0; i < pts.length - 1; i++) {
    if (s <= cum[i + 1] || i === pts.length - 2) {
      const seg = Math.max(1e-9, cum[i + 1] - cum[i]);
      const t = (s - cum[i]) / seg;
      return pts[i].clone().lerp(pts[i + 1], t);
    }
  }
  return pts[pts.length - 1].clone();
}

function arcOfPoint(pts, cum, p) {
  let best = Infinity, bestU = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1];
    const ab = b.clone().sub(a);
    const L2 = ab.lengthSq();
    let t = L2 > 1e-9 ? p.clone().sub(a).dot(ab) / L2 : 0;
    t = Math.max(0, Math.min(1, t));
    const q = a.clone().addScaledVector(ab, t);
    const d = q.distanceToSquared(p);
    if (d < best) { best = d; bestU = cum[i] + t * (cum[i + 1] - cum[i]); }
  }
  return bestU;
}

function orderChain(pts) {
  if (pts.length <= 2) return pts.slice();
  let start = 0, bestScore = -1;
  pts.forEach((p, i) => {
    let nn = Infinity;
    pts.forEach((q, j) => { if (i !== j) nn = Math.min(nn, p.distanceTo(q)); });
    if (nn > bestScore) { bestScore = nn; start = i; }
  });
  const left = pts.slice(), out = [left.splice(start, 1)[0]];
  while (left.length) {
    let bi = 0, bd = Infinity;
    left.forEach((p, i) => { const d = p.distanceTo(out[out.length - 1]); if (d < bd) { bd = d; bi = i; } });
    out.push(left.splice(bi, 1)[0]);
  }
  return out;
}

function splitChains(pts) {
  const n = pts.length;
  if (n <= 1) return [pts.slice()];
  const nn = pts.map((p, i) => {
    let d = Infinity;
    pts.forEach((q, j) => { if (i !== j) d = Math.min(d, p.distanceTo(q)); });
    return d;
  });
  const sorted = nn.slice().sort((a, b) => a - b);
  const med = sorted[Math.floor(sorted.length / 2)] || 0.5;
  const link = Math.max(0.6, med * 2.2);
  const par = pts.map((_, i) => i);
  const find = (a) => { while (par[a] !== a) { par[a] = par[par[a]]; a = par[a]; } return a; };
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (pts[i].distanceTo(pts[j]) < link) {
        const a = find(i), b = find(j);
        if (a !== b) par[a] = b;
      }
    }
  }
  const groups = new Map();
  pts.forEach((p, i) => {
    const r = find(i);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(p);
  });
  return Array.from(groups.values()).map(orderChain);
}

/* ---------- 中心线拟合：物体几何 → 世界坐标折线 ---------- */

function worldSamples(o, maxN) {
  const out = [];
  const p = new THREE.Vector3();
  const meshes = [];
  o.traverse(mm => { if (mm.isMesh && mm.geometry && mm.geometry.attributes.position) meshes.push(mm); });
  if (!meshes.length) return out;
  const per = Math.max(24, Math.floor((maxN || 600) / meshes.length));
  meshes.forEach(mm => {
    const pa = mm.geometry.attributes.position;
    const step = Math.max(1, Math.floor(pa.count / per));
    for (let i = 0; i < pa.count; i += step) {
      p.set(pa.getX(i), pa.getY(i), pa.getZ(i)).applyMatrix4(mm.matrixWorld);
      out.push(p.clone());
    }
    const i2 = pa.count - 1;
    p.set(pa.getX(i2), pa.getY(i2), pa.getZ(i2)).applyMatrix4(mm.matrixWorld);
    out.push(p.clone());
  });
  return out;
}

/* PCA 第一主方向（幂迭代，12 次足够） */
function principalAxis(pts, c) {
  let xx = 0, xy = 0, xz = 0, yy = 0, yz = 0, zz = 0;
  const d = new THREE.Vector3();
  pts.forEach(q => {
    d.copy(q).sub(c);
    xx += d.x * d.x; xy += d.x * d.y; xz += d.x * d.z;
    yy += d.y * d.y; yz += d.y * d.z; zz += d.z * d.z;
  });
  const n = pts.length || 1;
  xx /= n; xy /= n; xz /= n; yy /= n; yz /= n; zz /= n;
  let v = new THREE.Vector3(1, 0.31, 0.17).normalize();
  for (let it = 0; it < 12; it++) {
    const nx = xx * v.x + xy * v.y + xz * v.z;
    const ny = xy * v.x + yy * v.y + yz * v.z;
    const nz = xz * v.x + yz * v.y + zz * v.z;
    v = new THREE.Vector3(nx, ny, nz);
    if (v.lengthSq() < 1e-12) return new THREE.Vector3(0, 1, 0);
    v.normalize();
  }
  return v;
}

/* 物体中心线：主轴分箱取质心。
   直管 → 一串共线点；L 形弯管 → 拐角处质心自然落在弯头，且投影单调不锯齿。 */
function objectPolyline(o) {
  const pts = worldSamples(o, 600);
  if (pts.length < 8) return null;
  const c = new THREE.Vector3();
  pts.forEach(q => c.add(q));
  c.multiplyScalar(1 / pts.length);
  const ax = principalAxis(pts, c);

  let tmin = Infinity, tmax = -Infinity;
  const ts = pts.map(q => { const t = q.clone().sub(c).dot(ax); if (t < tmin) tmin = t; if (t > tmax) tmax = t; return t; });
  const len = tmax - tmin;
  if (!(len > 0.05)) return null;

  const nb = Math.max(4, Math.min(28, Math.ceil(len / 0.35)));
  const sum = new Array(nb).fill(null);
  for (let i = 0; i < pts.length; i++) {
    let b = Math.floor((ts[i] - tmin) / len * nb);
    if (b < 0) b = 0; if (b >= nb) b = nb - 1;
    if (!sum[b]) sum[b] = { n: 0, p: new THREE.Vector3() };
    sum[b].n++; sum[b].p.add(pts[i]);
  }
  const out = [];
  for (let b = 0; b < nb; b++) {
    if (!sum[b]) continue;
    out.push(sum[b].p.multiplyScalar(1 / sum[b].n));
  }
  if (out.length < 2) return null;

  /* 单调性检查：若质心序列出现明显回头（U 形管被主轴压扁），退回两点直线 */
  let back = 0;
  for (let i = 1; i < out.length; i++) {
    const d = out[i].clone().sub(out[i - 1]).dot(ax);
    if (d < -0.02) back++;
  }
  if (back > Math.max(1, out.length * 0.2)) {
    const a = out[0].clone(), b = out[out.length - 1].clone();
    return [a, b];
  }
  return out;
}

/* 组内折线首尾相接：贪心最近端点 + 统一方向 */
function linkPolylines(segs) {
  const n = segs.length;
  if (n === 1) return [{ si: 0, flip: false }];
  const used = new Array(n).fill(false);
  let start = 0, bestScore = -1;
  for (let i = 0; i < n; i++) {
    for (const e of [0, segs[i].length - 1]) {
      let nn = Infinity;
      for (let j = 0; j < n; j++) {
        if (i === j) continue;
        for (const f of [0, segs[j].length - 1]) nn = Math.min(nn, segs[i][e].distanceTo(segs[j][f]));
      }
      if (isFinite(nn) && nn > bestScore) { bestScore = nn; start = i; }
    }
  }
  const out = [];
  used[start] = true;
  out.push({ si: start, flip: false });
  let tail = segs[start][segs[start].length - 1];

  for (let k = 1; k < n; k++) {
    let bi = -1, be = 0, bd = Infinity;
    for (let j = 0; j < n; j++) {
      if (used[j]) continue;
      const s = segs[j];
      const d0 = tail.distanceTo(s[0]);
      const d1 = tail.distanceTo(s[s.length - 1]);
      if (d0 < bd) { bd = d0; bi = j; be = 0; }
      if (d1 < bd) { bd = d1; bi = j; be = 1; }
    }
    if (bi < 0) break;
    used[bi] = true;
    const flip = (be === 1);
    out.push({ si: bi, flip });
    const s2 = flip ? segs[bi].slice().reverse() : segs[bi];
    tail = s2[s2.length - 1];
  }
  return out;
}
