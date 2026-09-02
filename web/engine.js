/* ============================================================================
   浏览器端推理引擎。

   这个文件是 Python 后端的一份可运行副本，不是"演示用的假逻辑"。
   检索、BKT、自适应选题、辩论仲裁、审核规则、资源组装，全部按后端同一套
   规则重新实现，跑在同一份知识库和题库上。

   为什么值得做两遍：
     离线的 showcase.html 如果只能回放录好的会话，那它就只是一部片子。
     评委输入自己的背景、亲手答题，系统当场算出学情并生成资源 —— 这才叫演示。
     而"能不能真跑"恰恰是这类比赛最容易被现场戳穿的地方。

   一致性怎么保证：
     tests/test_parity.py 用同一批输入分别跑 Python 和这里的实现，
     比对掌握概率、选题顺序、审核判定。两边不一致就红。
     改了后端的规则，必须同步改这里，测试会盯着。

   刻意不做的事：
     不接大模型。浏览器里没有 key，也不该有。断言抽取用的是与 MockLLM
     完全相同的"按句切分 + 挂引用"策略，所以离线产出的是知识库原文的
     结构化重组，不是生成式改写。这一点界面上要写清楚，不能让人误以为
     离线模式下也有大模型在工作。
   ============================================================================ */

(function (global) {
"use strict";

/* ---------- 分词与相似度：对应 core/retrieval.py ---------- */

const CJK = /[\u4e00-\u9fff]/;
const ASCII = /[a-zA-Z0-9]+/g;

function tokenize(text) {
  const t = String(text || "").toLowerCase();
  const out = [];
  let m;
  ASCII.lastIndex = 0;
  while ((m = ASCII.exec(t)) !== null) out.push(m[0]);
  const cjk = [];
  for (const ch of t) if (CJK.test(ch)) cjk.push(ch);
  for (let i = 0; i < cjk.length - 1; i++) out.push(cjk[i] + cjk[i + 1]);
  for (const c of cjk) out.push(c);
  return out;
}

function overlapRatio(claim, chunkText) {
  const a = new Set(tokenize(claim));
  const b = new Set(tokenize(chunkText));
  if (!a.size) return 0;
  let hit = 0;
  for (const t of a) if (b.has(t)) hit++;
  return hit / a.size;
}

function jaccard(x, y) {
  const a = new Set(tokenize(x)), b = new Set(tokenize(y));
  if (!a.size || !b.size) return 0;
  let inter = 0;
  for (const t of a) if (b.has(t)) inter++;
  return inter / (a.size + b.size - inter);
}

/* 中文数字。与 core/retrieval.py 的 cn_to_int 对应。 */
const CN_D = {"零":0,"〇":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,
              "六":6,"七":7,"八":8,"九":9};
const CN_U = {"十":10,"百":100,"千":1000,"万":10000,"亿":100000000};

function cnToInt(s) {
  if (!s) return null;
  let total = 0, section = 0, digit = null;
  for (const ch of s) {
    if (ch in CN_D) digit = CN_D[ch];
    else if (ch in CN_U) {
      const u = CN_U[ch];
      if (u >= 10000) { section = (section + (digit || 0)) * u; total += section; section = 0; digit = null; }
      else { section += (digit === null ? 1 : digit) * u; digit = null; }
    } else return null;
  }
  return total + section + (digit || 0);
}

const NUM_RE = /\d+(?:\.\d+)?/g;
const CN_NUM_RE = /[零〇一二两三四五六七八九十百千万亿]{1,12}/g;

function numbersIn(text) {
  const out = new Set();
  const t = String(text || "");
  let m;
  NUM_RE.lastIndex = 0;
  while ((m = NUM_RE.exec(t)) !== null) out.add(m[0]);
  CN_NUM_RE.lastIndex = 0;
  while ((m = CN_NUM_RE.exec(t)) !== null) {
    const v = cnToInt(m[0]);
    if (v !== null && v > 0) out.add(String(v));
  }
  return out;
}

/* ---------- 知识库索引 ---------- */

class Retriever {
  constructor(kb) {
    this.chunks = Object.keys(kb).map(id => Object.assign({id}, kb[id]));
    this.byId = kb;
    this.df = new Map();
    this.charDf = new Map();
    for (const c of this.chunks) {
      const seen = new Set(tokenize(c.title + " " + c.text));
      for (const t of seen) this.df.set(t, (this.df.get(t) || 0) + 1);
      const chars = new Set([...(c.title + " " + c.text)].filter(x => CJK.test(x)));
      for (const ch of chars) this.charDf.set(ch, (this.charDf.get(ch) || 0) + 1);
    }
    this.n = this.chunks.length;
    this._distinct = null;
  }

  byKp(kp) { return this.chunks.filter(c => c.kp === kp); }
  get(id) { const c = this.byId[id]; return c ? Object.assign({id}, c) : null; }

  /* 与后端同一口径：低频二元组，且两个字本身都不是高频虚字 */
  distinctive() {
    if (this._distinct) return this._distinct;
    const cap = Math.max(1, Math.floor(this.n * 0.35));
    const s = new Set();
    for (const [t, df] of this.df) {
      if (df > 3 || t.length !== 2) continue;
      if (!CJK.test(t[0]) || !CJK.test(t[1])) continue;
      if ((this.charDf.get(t[0]) || 0) > cap) continue;
      if ((this.charDf.get(t[1]) || 0) > cap) continue;
      s.add(t);
    }
    this._distinct = s;
    return s;
  }

  /* BM25 简化版：够用于宽检索的排序 */
  search(query, topK, kp) {
    const q = tokenize(query);
    const scored = [];
    for (const c of this.chunks) {
      if (kp && c.kp !== kp) continue;
      const toks = tokenize(c.title + " " + c.text);
      const tf = new Map();
      for (const t of toks) tf.set(t, (tf.get(t) || 0) + 1);
      let sc = 0;
      for (const t of q) {
        const f = tf.get(t) || 0;
        if (!f) continue;
        const df = this.df.get(t) || 1;
        sc += Math.log(1 + (this.n - df + 0.5) / (df + 0.5)) * f / (f + 1.2);
      }
      if (sc > 0) scored.push([c, sc]);
    }
    scored.sort((a, b) => b[1] - a[1]);
    return scored.slice(0, topK);
  }
}

/* ---------- BKT：对应 core/bkt.py ---------- */

const BKT = {
  update(pL, correct, P) {
    let num, den;
    if (correct) { num = pL * (1 - P.p_S); den = num + (1 - pL) * P.p_G; }
    else { num = pL * P.p_S; den = num + (1 - pL) * (1 - P.p_G); }
    const post = den > 0 ? num / den : pL;
    return post + (1 - post) * P.p_T;
  },
  confidence(n) { return n <= 0 ? 0 : Math.round((1 - Math.pow(0.6, n)) * 1000) / 1000; },
  prior(bg, base) {
    base = base === undefined ? 0.20 : base;
    const edu = String(bg.education || "");
    const hours = Number(bg.hands_on_hours || 0);
    let p = base;
    if (edu.indexOf("博士") >= 0 || edu.indexOf("硕士") >= 0) p += 0.15;
    else if (edu.indexOf("本科") >= 0) p += 0.10;
    else if (edu.indexOf("高职") >= 0 || edu.indexOf("专科") >= 0) p += 0.05;
    if (hours > 0) p += Math.min(0.20, 0.065 * Math.sqrt(hours) / 2.0);
    return Math.max(0.05, Math.min(0.85, p));
  }
};

/* ---------- 自适应测评：对应 core/cat.py ---------- */

const PROBE_HIGH = 0.65, PROBE_LOW = 0.35, MAX_PROBES = 1;

class Adaptive {
  constructor(items, kps, background, opts) {
    opts = opts || {};
    this.items = {};
    this.byKp = {};
    for (const it of items) {
      // 与 core.demo_items.formal_demo_items 保持一致：原题库可保留待改题，
      // 但浏览器端正式测评不能把它们重新纳入候选集。
      if (it.demo_eligible === false) continue;
      this.items[it.id] = it;
      (this.byKp[it.kp] = this.byKp[it.kp] || []).push(it);
    }
    for (const k in this.byKp) this.byKp[k].sort((a, b) => a.level - b.level);
    this.kps = {};
    for (const k of kps) this.kps[k.id] = k;
    this.P = opts.params || {p_T:0.15, p_S:0.10, p_G:0.25};
    this.prior = BKT.prior(background);
    this.maxItems = opts.maxItems || 16;
    this.targetConf = opts.targetConf || 0.75;
    this.blindTh = opts.blindTh === undefined ? 0.25 : opts.blindTh;
    this.R = opts.retriever || null;        // 给了才能现场命题
    this.maxGenerated = opts.maxGenerated === undefined ? 8 : opts.maxGenerated;
    this.generated = [];
    this.rejects = [];
    this.state = {};
    for (const k in this.kps) {
      this.state[k] = {p:this.prior, n:0, correct:0, probes:0,
                       curve:[Math.round(this.prior*1000)/1000]};
    }
    this.asked = [];
    this.log = [];
    this.pendingProbe = null;
  }

  _unasked(kp) {
    return (this.byKp[kp] || []).filter(i => this.asked.indexOf(i.id) < 0);
  }
  _pCorrect(pL) { return pL * (1 - this.P.p_S) + (1 - pL) * this.P.p_G; }
  _info(pL) { return 1 - Math.abs(this._pCorrect(pL) - 0.5) * 2; }

  /* 前置已确认为盲区的知识点不再实测，直接推断。与 core/cat.py 同一规则。 */
  blockedByPrereq() {
    const out = {};
    for (const kp in this.kps) {
      for (const pre of (this.kps[kp].prereq || [])) {
        const st = this.state[pre];
        if (st && st.n >= 1 && st.p < this.blindTh) { out[kp] = pre; break; }
      }
    }
    return out;
  }

  done() {
    if (this.asked.length >= this.maxItems) return true;
    const blocked = this.blockedByPrereq();
    const need = Object.keys(this.state).filter(k => !(blocked[k] && this.state[k].n === 0));
    for (const k of need) if (this.state[k].n === 0) return false;
    for (const k of need)
      if (BKT.confidence(this.state[k].n) < this.targetConf) return false;
    return true;
  }

  next() {
    if (this.asked.length >= this.maxItems) return null;
    if (this.pendingProbe) {
      const kp = this.pendingProbe;
      this.pendingProbe = null;
      const cand = this._unasked(kp);
      if (cand.length) {
        const st = this.state[kp];
        let best = cand[0], bd = Infinity;
        for (const it of cand) {
          const d = Math.abs(this._pCorrect(st.p) - 0.5);
          if (d < bd) { bd = d; best = it; }
        }
        return Object.assign({}, best, {_reason:"probe", _kp_name:this.kps[kp].name});
      }
    }
    /* 某个知识点题库问光了但证据还不够 —— 固定题库的天花板，交给现场命题。 */
    const starved = this._starved();
    if (starved && this.R && this.generated.length < this.maxGenerated) {
      const g = this._generateFor(starved);
      if (g) return Object.assign({}, g, {_reason:"generated", _kp_name:this.kps[g.kp].name});
    }

    if (this.done()) return null;

    const blocked = this.blockedByPrereq();
    let best = null, bestScore = -1;
    for (const kp in this.state) {
      const st = this.state[kp];
      if (blocked[kp] && st.n === 0) continue;     // 由前置推断，不占题量
      const cand = this._unasked(kp);
      if (!cand.length) continue;
      const info = this._info(st.p);
      const first = st.n === 0 ? 0.22 : 0;
      const pre = this.kps[kp].prereq || [];
      let gate = 1;
      for (const p of pre) if (this.state[p] && this.state[p].n === 0) gate = 0.75;
      const score = (info + first) * gate;
      for (const it of cand) {
        const fit = 1 - Math.abs(it.level - (1 + st.p * 4)) / 4;
        const s = score * (0.7 + 0.3 * Math.max(0, fit));
        if (s > bestScore) { bestScore = s; best = it; }
      }
    }
    if (!best) return null;
    /* 现成题的难度离估计水平太远时，现场出一道更贴合的 */
    if (this.R && this.generated.length < this.maxGenerated) {
      const want = this._targetLevel(best.kp);
      if (Math.abs(best.level - want) > 1) {
        const g = this._generateFor(best.kp, want);
        if (g) return Object.assign({}, g, {_reason:"generated", _kp_name:this.kps[g.kp].name});
      }
    }
    return Object.assign({}, best, {_reason:"select", _kp_name:this.kps[best.kp].name});
  }

  _targetLevel(kp) {
    return Math.max(1, Math.min(5, Math.round(1 + this.state[kp].p * 4)));
  }

  _starved() {
    const cand = Object.keys(this.state).filter(kp =>
      this.state[kp].n >= 1 &&
      BKT.confidence(this.state[kp].n) < this.targetConf &&
      !this._unasked(kp).length);
    if (!cand.length) return null;
    let best = cand[0], bd = Infinity;
    for (const kp of cand) {
      const d = Math.abs(this._pCorrect(this.state[kp].p) - 0.5);
      if (d < bd) { bd = d; best = kp; }
    }
    return best;
  }

  _generateFor(kp, want) {
    const avoid = new Set(this.asked.map(i => this.items[i] && this.items[i].stem));
    const it = makeItem(kp, want || this._targetLevel(kp), this.R, this.kps,
                        avoid, "s" + this.asked.length);
    if (!it) return null;
    if (it._rejects) this.rejects.push.apply(this.rejects, it._rejects);
    this.items[it.id] = it;
    (this.byKp[kp] = this.byKp[kp] || []).push(it);
    this.generated.push(it.id);
    return it;
  }

  answer(itemId, choice) {
    const item = this.items[itemId];
    const kp = item.kp, st = this.state[kp];
    const correct = choice === item.answer;
    /* 生成题过了审核，但实际区分度未经验证，用更保守的失误率给证据打折 */
    const P = item.origin === "generated"
      ? {p_T:this.P.p_T, p_S:0.18, p_G:this.P.p_G} : this.P;
    const before = st.p;
    const pred = before * (1 - P.p_S) + (1 - before) * P.p_G;
    st.p = BKT.update(before, correct, P);
    st.n += 1;
    st.correct += correct ? 1 : 0;
    st.curve.push(Math.round(st.p * 1000) / 1000);
    this.asked.push(itemId);

    let probe = "";
    /* 与 core/cat.py 同一门槛：必须已有该知识点的证据才追问。
       否则先验偏低时第一题答对就触发，追的是跟先验的冲突，消解不了歧义。 */
    if ((st.n - 1) >= 1 && st.probes < MAX_PROBES && this._unasked(kp).length) {
      if (before >= PROBE_HIGH && !correct)
        probe = "估计已掌握却答错，需分辨是一次失误还是确实没掌握";
      else if (before <= PROBE_LOW && correct)
        probe = "估计为盲区却答对，需分辨是真会还是四选一蒙中";
    }
    if (probe) { this.pendingProbe = kp; st.probes += 1; }

    const step = {
      item_id:itemId, kp, kp_name:this.kps[kp].name, correct,
      predicted:Math.round(pred*1000)/1000,
      before:Math.round(before*1000)/1000,
      after:Math.round(st.p*1000)/1000,
      delta:Math.round((st.p-before)*1000)/1000,
      probe, confidence:BKT.confidence(st.n),
      level:item.level, origin:item.origin || "bank", source_id:item.source_id,
      stem:item.stem, options:item.options, answer:item.answer, choice
    };
    this.log.push(step);
    return step;
  }

  /* 生成与后端 Diagnosis 同构的诊断结果 */
  diagnose(cfg) {
    const blocked = this.blockedByPrereq();
    const mastery = [];
    for (const kp in this.state) {
      const st = this.state[kp], node = this.kps[kp];
      let status;
      if (st.n === 0) status = "blind";
      else if (st.p < cfg.mastery_blind) status = "blind";
      else if (st.p < cfg.mastery_weak) status = "weak";
      else if (st.p < cfg.mastery_ok) status = "ok";
      else status = "strong";
      const iv = masteryInterval(st.correct, st.n, this.P);
      const lk = st.n ? luckProbability(st.correct, st.n, this.P.p_G) : 1;
      const es = evidenceState(st.p, iv[0], lk, st.n, cfg.mastery_ok, cfg.mastery_blind);
      mastery.push({kp, name:node.name, level:node.level, asked:st.n,
                    correct:st.correct, score:Math.round(st.p*1000)/1000,
                    status, confidence:BKT.confidence(st.n), curve:st.curve,
                    lower:iv[0], upper:iv[1], luck:Math.round(lk*10000)/10000,
                    evidence:es[0], evidence_why:es[1],
                    inferred_from: (st.n === 0 && blocked[kp]) ? blocked[kp] : null});
    }
    const gapIds = mastery.filter(m => m.status === "blind" || m.status === "weak")
                          .map(m => m.kp);
    const gaps = this._topo(gapIds);
    const tested = mastery.filter(m => m.asked > 0);
    const overall = tested.length
      ? Math.round(tested.reduce((a,m) => a+m.score, 0) / tested.length * 1000) / 1000 : 0;
    return {
      profile_id:"LIVE", mastery, gaps, overall,
      entry_level:Math.max(1, Math.min(5, 1 + Math.floor(overall*3))),
      prior:Math.round(this.prior*1000)/1000,
      low_confidence:mastery.filter(m => m.asked > 0 && m.confidence < 0.75).map(m => m.kp),
      narrative:this._narrate(mastery, gaps)
    };
  }

  _topo(ids) {
    const rest = ids.slice(), out = [];
    let guard = 0;
    while (rest.length && guard++ < 100) {
      const layer = rest.filter(kp =>
        (this.kps[kp].prereq || []).every(p => rest.indexOf(p) < 0));
      const use = layer.length ? layer : rest.slice();
      use.sort((a,b) => this.kps[a].level - this.kps[b].level || (a<b?-1:1));
      for (const k of use) out.push(k);
      for (const k of use) rest.splice(rest.indexOf(k), 1);
    }
    return out;
  }

  _narrate(mastery, gaps) {
    const strong = mastery.filter(m => m.status === "strong").map(m => m.name);
    const gapNames = gaps.map(g => this.kps[g].name);
    const untested = mastery.filter(m => m.asked === 0).length;
    let s = "";
    if (strong.length) s += "已具备基础的是：" + strong.slice(0,4).join("、") + "。";
    if (gapNames.length) s += "需要优先补齐的是：" + gapNames.slice(0,5).join("、") + "。";
    if (untested) s += `本次测评未覆盖 ${untested} 个知识点，按保守策略一并计入盲区。`;
    s += "学习顺序按知识点前置关系排列。";
    return s;
  }
}

/* ---------- 现场命题：对应 agents/examiner.py ---------- */

const ITEM_ANSWER_MIN = 0.55, ITEM_DISTRACTOR_MAX = 0.50;
const UNITS_RE = /^\s*(?:[零〇一二两三四五六七八九十百千万亿]+|[\d.]+)\s*(?:毫米每秒|毫米|厘米|米|秒|分钟|小时|天|周|个?月|年|度|层|次|个|倍|%|％|千克|公斤|牛|安|伏)?\s*$/;
/* 量值：数字后必须跟单位量词，前面不能贴字母或连字符。
   不加这条，「SRVO-001」的 001、「J1至J3」的 3 会被当成量值出题。 */
const QUANTITY_RE = /(?<![A-Za-z0-9-])(\d+(?:\.\d+)?)\s*(?=毫米每秒|毫米|厘米|米|秒|分钟|小时|天|周|个?月|年|度|层|次|台|根|个|倍|%|％|千克|公斤|牛|安|伏)/g;

function isNumericOption(t) { return !!String(t || "").trim() && UNITS_RE.test(String(t).trim()); }

function quantities(text) {
  const out = []; let m;
  QUANTITY_RE.lastIndex = 0;
  while ((m = QUANTITY_RE.exec(text)) !== null) out.push(m[1]);
  return out;
}

/* 审核四关，与 ExaminerAgent.vet 同一套判据。返回 null 表示通过。 */
function vetItem(item, R) {
  const opts = item.options || [];
  if (opts.length < 3) return "选项不足三个";
  if (!Number.isInteger(item.answer) || item.answer < 0 || item.answer >= opts.length)
    return "正确答案序号越界";
  if (new Set(opts).size !== opts.length) return "选项存在重复";
  const chunk = R.get(item.source_id || "");
  if (!chunk) return "引用切片不存在";
  const body = chunk.title + " " + chunk.text;
  const pool = numbersIn(body);
  const right = opts[item.answer];

  if (isNumericOption(right)) {
    const miss = [...numbersIn(right)].filter(x => !pool.has(x));
    if (miss.length) return `正确答案的数值 ${miss.join("、")} 在切片中不存在`;
  } else if (overlapRatio(right, body) < ITEM_ANSWER_MIN) {
    return "正确答案缺少知识库依据";
  }

  for (let i = 0; i < opts.length; i++) {
    if (i === item.answer) continue;
    if (isNumericOption(opts[i])) {
      const n = numbersIn(opts[i]);
      if (n.size && [...n].every(x => pool.has(x)))
        return `干扰项「${opts[i]}」的数值全部见于切片，可能同样成立`;
    } else if (overlapRatio(opts[i], body) >= ITEM_DISTRACTOR_MAX) {
      return `干扰项「${opts[i]}」可能同样成立`;
    }
  }

  const extra = [...numbersIn(item.stem)].filter(x => !pool.has(x));
  if (extra.length) return `题干出现切片外数值 ${extra.join("、")}`;

  if (!opts.every(isNumericOption)) {
    const lens = opts.map(o => o.length);
    const mx = Math.max(...lens), mn = Math.min(...lens);
    if (lens[item.answer] === mx && mn > 0 && mx >= mn * 2) return "存在长度线索";
  }
  return null;
}

/* 离线命题：把切片里的量值挖空做成四选一，干扰项由原值倍数变换而来。
   题目完全来自知识库，答案就是原文。照样要过 vetItem。
   局限：只能出数值类题目，出不了概念辨析题 —— 接真模型后覆盖面会宽得多。 */
function makeItem(kp, difficulty, R, kpIndex, avoid, seedBase) {
  const chunks = R.byKp(kp);
  if (!chunks.length) return null;
  const rejects = [];
  for (let a = 0; a < 3; a++) {
    const chunk = chunks[a % chunks.length];
    const qs = quantities(chunk.text);
    if (!qs.length) continue;
    const seed = (seedBase || "") + kp + difficulty + a;
    let h = 0;
    for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const raw = qs[h % qs.length];
    const sent = splitSentences(chunk.text).find(s => s.indexOf(raw) >= 0) || chunk.text.slice(0, 60);
    const val = parseFloat(raw);
    if (!isFinite(val)) continue;
    const facs = [[2, 0.5, 4], [0.25, 3, 1.5], [5, 0.2, 2.5]][h % 3];
    const fmt = raw.indexOf(".") >= 0
      ? v => String(parseFloat(v.toFixed(1)))
      : v => String(Math.round(v));
    const opts = [raw];
    for (const f of facs) { const w = fmt(val * f); if (opts.indexOf(w) < 0) opts.push(w); }
    if (opts.length < 4) continue;
    const four = opts.slice(0, 4);
    const idx = h % 4;
    const tmp = four[0]; four[0] = four[idx]; four[idx] = tmp;
    const item = {
      id: `G-${kp}-${difficulty}-${a}`, kp, level: difficulty,
      stem: "依据规范，填入正确数值：" + sent.replace(raw, "____"),
      options: four, answer: idx, source_id: chunk.id, origin: "generated",
      explain: `原文为 ${raw}，见 ${chunk.id}。`
    };
    if (avoid && avoid.has(item.stem)) continue;
    const why = vetItem(item, R);
    if (why) { rejects.push({item, why}); continue; }
    item._rejects = rejects;
    return item;
  }
  return null;
}

/* ---------- 证据强度：区间与蒙对概率，对应 core/bkt.py ---------- */

function logComb(n, k) {
  let r = 0;
  for (let i = 1; i <= k; i++) r += Math.log(n - k + i) - Math.log(i);
  return r;
}

/* 完全不会的人靠蒙达到该成绩（或更好）的概率。二项分布右尾。
   这是给学习者看的最直观的一个数 ——
   「你答对 2/2，但瞎蒙也有 6.2% 的机会考成这样」。 */
function luckProbability(correct, asked, pg) {
  pg = pg === undefined ? 0.25 : pg;
  if (asked <= 0) return 1;
  correct = Math.max(0, Math.min(correct, asked));
  let t = 0;
  for (let i = correct; i <= asked; i++)
    t += Math.exp(logComb(asked, i) + i * Math.log(pg) + (asked - i) * Math.log(1 - pg));
  return Math.min(1, t);
}

/* Beta 分位数，网格法。不引第三方库，且便于逐行核对。 */
function betaQuantile(a, b, q, steps) {
  steps = steps || 2000;
  const lw = [];
  let m = -Infinity;
  for (let i = 0; i < steps; i++) {
    const x = (i + 0.5) / steps;
    const v = (a - 1) * Math.log(x) + (b - 1) * Math.log(1 - x);
    lw.push(v);
    if (v > m) m = v;
  }
  let tot = 0;
  const w = lw.map(v => { const e = Math.exp(v - m); tot += e; return e; });
  let acc = 0;
  for (let i = 0; i < steps; i++) {
    acc += w[i] / tot;
    if (acc >= q) return (i + 0.5) / steps;
  }
  return 1 - 0.5 / steps;
}

/* 掌握概率的可信区间。把作答正确率的后验反解回掌握概率，
   反解这一步正是"扣掉蒙对成分"。与 core/bkt.py 同一口径。 */
function masteryInterval(correct, asked, P, level) {
  level = level === undefined ? 0.80 : level;
  if (asked <= 0) return [0, 1];
  const denom = 1 - P.p_S - P.p_G;
  if (denom <= 0) return [0, 1];
  const a = 1 + correct, b = 1 + (asked - correct);
  const out = [(1 - level) / 2, 1 - (1 - level) / 2].map(q => {
    const th = betaQuantile(a, b, q);
    return Math.max(0, Math.min(1, (th - P.p_G) / denom));
  });
  return [Math.round(out[0] * 1000) / 1000, Math.round(out[1] * 1000) / 1000];
}

const LUCK_MAX = 0.05;

function evidenceState(score, lower, luck, asked, okTh, blindTh) {
  if (asked <= 0) return ["untested", "未作答"];
  if (score >= okTh) {
    if (lower >= okTh && luck <= LUCK_MAX)
      return ["confirmed_strong", `下界 ${lower.toFixed(2)} 过线，蒙对概率 ${(luck*100).toFixed(1)}%`];
    const why = [];
    if (lower < okTh) why.push(`区间下界仅 ${lower.toFixed(2)}`);
    if (luck > LUCK_MAX) why.push(`纯蒙也有 ${(luck*100).toFixed(0)}% 概率考成这样`);
    return ["likely_strong", why.join("；")];
  }
  if (score < blindTh)
    return asked >= 2
      ? ["confirmed_blind", `${asked} 题作答，掌握概率 ${score.toFixed(2)}`]
      : ["likely_blind", "仅 1 题作答，样本不足"];
  return ["inconclusive", `掌握概率 ${score.toFixed(2)} 处于中间区间`];
}

/* ---------- 能力图谱：对应 core/ability.py ---------- */

function buildAbility(diag, dims) {
  const byKp = {};
  for (const m of diag.mastery) byKp[m.kp] = m;
  const out = {dims: [], untested_dims: []};
  for (const d of dims) {
    const kps = d.kps.filter(k => byKp[k]);
    const tested = kps.map(k => byKp[k]).filter(m => m.asked > 0);
    let score = 0, lower = 0, worst = null;
    if (tested.length) {
      score = tested.reduce((a, m) => a + m.score, 0) / tested.length;
      lower = tested.reduce((a, m) => a + (m.lower || 0), 0) / tested.length;
      worst = tested.reduce((a, m) => (m.score < a.score ? m : a), tested[0]);
    } else out.untested_dims.push(d.name);
    out.dims.push({
      id: d.id, name: d.name, desc: d.desc || "",
      score: Math.round(score * 1000) / 1000,
      lower: Math.round(lower * 1000) / 1000,
      tested: tested.length, total: kps.length,
      gap: Math.round((score - lower) * 1000) / 1000,
      worst_name: worst ? worst.name : "",
    });
  }
  const live = out.dims.filter(d => d.tested);
  out.overall = live.length ? Math.round(live.reduce((a, d) => a + d.score, 0) / live.length * 1000) / 1000 : 0;
  out.overall_lower = live.length ? Math.round(live.reduce((a, d) => a + d.lower, 0) / live.length * 1000) / 1000 : 0;
  return out;
}

/* ---------- 断言抽取：与 MockLLM 的策略完全一致 ---------- */

const SENT = /[^。！？；\n]+[。！？；]?/g;

function splitSentences(text) {
  const out = [];
  let m;
  SENT.lastIndex = 0;
  while ((m = SENT.exec(text)) !== null) {
    const s = m[0].trim();
    if (s.length >= 8) out.push(s);
  }
  return out;
}

function draftClaims(chunks, n) {
  const claims = [];
  for (const c of chunks) {
    for (const s of splitSentences(c.text)) {
      claims.push({text:s, source_id:c.id});
      if (claims.length >= n) return claims;
    }
  }
  return claims;
}

/* ---------- 审核裁判：对应 agents/audit.py ---------- */

const EVIDENCE_MIN = 0.42, MISATTRIB_MARGIN = 0.15;
const SCOPE_EXPANSION_CUES = [
  "所有", "任何情况下", "任何品牌", "一律", "无论", "都必须", "都使用"
];
const SCOPE_LIMIT_CUES = [
  "为例", "仅适用", "仅在", "因控制器而异", "实际机型", "具体机型",
  "机型手册", "现场规程", "通用数值标准"
];
const RELAXATION_CUES = [
  "无需", "不必", "不需要", "不用", "完全替代", "可以省略"
];
const AUTOMATIC_COMPLETION_CUES = [
  "自动验证", "自动核对", "自动确认", "自动校验"
];
const REQUIREMENT_CUES = [
  "必须验证", "必须核对", "必须确认", "需要验证", "需要核对", "需要确认",
  "应当验证", "应当核对", "应当确认", "仍需验证", "仍需核对", "仍需确认",
  "另行验证", "前提满足"
];
const RISK_REQUIREMENT_RE = /(?:错误|不正确)(?:的)?[^，,。.;；]{0,16}(?:时)?可能(?:导致|触发|造成|引发|带来|引起)/;
const OMISSION_RISK_RE = /(?:未|不|没有|省略)[^，,。.;；]{0,16}(?:验证|核对|确认|满足)[^，,。.;；]{0,16}(?:会|将|可能)(?:导致|触发|造成|引发|带来|引起)/;

function semanticBoundaryIssue(claimText, evidenceText) {
  claimText = String(claimText || "").replace(/\s+/gu, "");
  evidenceText = String(evidenceText || "").replace(/\s+/gu, "");

  const expansionPositions = SCOPE_EXPANSION_CUES
    .filter(cue => claimText.includes(cue)).map(cue => claimText.indexOf(cue));
  const claimLimitPositions = SCOPE_LIMIT_CUES
    .filter(cue => claimText.includes(cue)).map(cue => claimText.indexOf(cue));
  const expandsScope = expansionPositions.length > 0;
  const limitsScope = SCOPE_LIMIT_CUES.some(cue => evidenceText.includes(cue));
  const preservesScope = claimLimitPositions.length > 0 && expansionPositions.every(
    expansionPos => claimLimitPositions.some(limitPos => limitPos < expansionPos)
  );
  if (expandsScope && limitsScope && !preservesScope)
    return "断言把资料限定的适用范围扩大成了无条件通用结论";

  const relaxationCues = RELAXATION_CUES.concat(AUTOMATIC_COMPLETION_CUES);
  const relaxation = relaxationCues.find(cue => claimText.includes(cue));
  const evidenceRelaxes = relaxationCues.some(cue => evidenceText.includes(cue));
  const hasRequirement = REQUIREMENT_CUES.some(cue => evidenceText.includes(cue)) ||
    RISK_REQUIREMENT_RE.test(evidenceText) || OMISSION_RISK_RE.test(evidenceText);
  if (relaxation && !evidenceRelaxes && hasRequirement)
    return `断言用“${relaxation}”取消了资料明确保留的条件或步骤`;
  return null;
}

function auditOne(claim, R) {
  if (!claim.source_id) return {verdict:"unsupported", note:"未给出知识库引用", ratio:0};
  const chunk = R.get(claim.source_id);
  if (!chunk) return {verdict:"unsupported", note:`引用的切片 ${claim.source_id} 不存在`, ratio:0};
  const full = chunk.title + " " + chunk.text;
  const ratio = overlapRatio(claim.text, chunk.text);

  const cn = numbersIn(claim.text), kn = numbersIn(full);
  const extra = [...cn].filter(x => !kn.has(x));
  if (extra.length)
    return {verdict:"contradicted", note:`断言中的数值 ${extra.join("、")} 在所引切片中不存在`, ratio};

  const boundaryIssue = semanticBoundaryIssue(claim.text, full);
  if (boundaryIssue)
    return {verdict:"contradicted", note:boundaryIssue, ratio};

  if (ratio < EVIDENCE_MIN)
    return {verdict:"unsupported", note:`与所引切片的证据覆盖率仅 ${ratio.toFixed(2)}`, ratio};

  const dist = R.distinctive();
  const inChunk = new Set(tokenize(full));
  const missing = [...new Set(tokenize(claim.text))].filter(t => dist.has(t) && !inChunk.has(t));
  if (missing.length >= 1)
    return {verdict:"contradicted", note:`断言使用的术语 ${missing.join("、")} 不属于所引切片`, ratio};

  let bestId = "", best = 0;
  for (const o of R.chunks) {
    const r = overlapRatio(claim.text, o.title + " " + o.text);
    if (r > best) { best = r; bestId = o.id; }
  }
  if (bestId !== chunk.id && best - ratio >= MISATTRIB_MARGIN)
    return {verdict:"contradicted",
            note:`切片 ${bestId} 的支撑度 ${best.toFixed(2)} 明显高于所引 ${chunk.id} 的 ${ratio.toFixed(2)}，疑似引用错位`,
            ratio};

  return {verdict:"supported", note:`证据覆盖率 ${ratio.toFixed(2)}`, ratio};
}

function review(claims, R) {
  const kept = [], dropped = [];
  for (const c of claims) {
    const r = auditOne(c, R);
    c.verdict = r.verdict;
    c.audit_note = r.note;
    c.evidence_score = Math.round(r.ratio * 1000) / 1000;
    (r.verdict === "supported" ? kept : dropped).push(c);
  }
  return {kept, dropped};
}

/* ---------- 交叉验证与辩论：对应 agents/debate.py ---------- */

const ALIGN_MIN = 0.55, ALIGN_MIN_SAME_SRC = 0.35, CONFLICT_MARGIN = 0.12;

function evidenceOf(claim, R) {
  let best = 0;
  for (const c of R.chunks) best = Math.max(best, overlapRatio(claim.text, c.title + " " + c.text));
  return best;
}

function numericSupport(claim, R) {
  const nums = numbersIn(claim.text);
  if (!nums.size) return [0, 0];
  const pool = new Set();
  let bestC = null, best = 0;
  for (const c of R.chunks) {
    const r = overlapRatio(claim.text, c.title + " " + c.text);
    if (r > best) { best = r; bestC = c; }
  }
  if (bestC) for (const x of numbersIn(bestC.title + " " + bestC.text)) pool.add(x);
  if (claim.source_id) {
    const cited = R.get(claim.source_id);
    if (cited) for (const x of numbersIn(cited.title + " " + cited.text)) pool.add(x);
  }
  let hit = 0;
  for (const x of nums) if (pool.has(x)) hit++;
  return [hit, nums.size];
}

function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

function arbitrate(left, right, R) {
  const nl = numericSupport(left, R), nr = numericSupport(right, R);
  const ln = numbersIn(left.text), rn = numbersIn(right.text);
  if (!sameSet(ln, rn) && (nl[1] || nr[1])) {
    if (nl[0] !== nr[0]) {
      const winLeft = nl[0] > nr[0];
      const win = winLeft ? left : right, lose = winLeft ? right : left;
      win.consensus = "arbitrated";
      win.rival = lose.text;
      return [win, `数值裁决：采纳方 ${Math.max(nl[0],nr[0])} 个数值有知识库依据，对立方仅 ${Math.min(nl[0],nr[0])} 个，对立说法已记录备查`];
    }
    return [null, `双方数值均无法在知识库中确认，无法判定，双方均不采纳`];
  }
  const el = evidenceOf(left, R), er = evidenceOf(right, R);
  if (Math.abs(el - er) < CONFLICT_MARGIN)
    return [null, `双方证据分接近（甲 ${el.toFixed(2)} / 乙 ${er.toFixed(2)}），无法判定，双方均不采纳`];
  const winLeft = el > er;
  const win = winLeft ? left : right, lose = winLeft ? right : left;
  win.consensus = "arbitrated";
  win.rival = lose.text;
  return [win, `证据分 ${Math.max(el,er).toFixed(2)} 胜 ${Math.min(el,er).toFixed(2)}，采纳前者，对立说法已记录备查`];
}

function debate(left, right, R) {
  const cand = [];
  for (let i = 0; i < left.length; i++)
    for (let j = 0; j < right.length; j++) {
      const s = jaccard(left[i].text, right[j].text);
      const sameSrc = left[i].source_id && left[i].source_id === right[j].source_id;
      if (s >= (sameSrc ? ALIGN_MIN_SAME_SRC : ALIGN_MIN)) cand.push([s, i, j]);
    }
  cand.sort((a, b) => b[0] - a[0]);
  const usedL = new Set(), usedR = new Set(), pairs = [];
  for (const [s, i, j] of cand) {
    if (usedL.has(i) || usedR.has(j)) continue;
    usedL.add(i); usedR.add(j);
    pairs.push([left[i], right[j], s]);
  }

  const out = [], log = {agreed:[], arbitrated:[], dropped:[], singles:[]};
  for (const [lc, rc, s] of pairs) {
    const same = sameSet(numbersIn(lc.text), numbersIn(rc.text));
    if (s >= 0.85 && lc.source_id === rc.source_id && same) {
      lc.consensus = "both";
      lc.proposed_by = ["专家甲", "专家乙"];
      out.push(lc);
      log.agreed.push({text:lc.text, sim:Math.round(s*1000)/1000, source:lc.source_id});
    } else {
      const [win, why] = arbitrate(lc, rc, R);
      const entry = {"甲":lc.text, "乙":rc.text, sim:Math.round(s*1000)/1000, why};
      if (!win) { log.dropped.push(entry); continue; }
      win.proposed_by = [win === lc ? "专家甲" : "专家乙"];
      entry["采纳"] = win.text;
      out.push(win);
      log.arbitrated.push(entry);
    }
  }
  for (let i = 0; i < left.length; i++) if (!usedL.has(i)) {
    left[i].consensus = "single"; left[i].proposed_by = ["专家甲"];
    out.push(left[i]); log.singles.push({by:"甲", text:left[i].text, source:left[i].source_id});
  }
  for (let j = 0; j < right.length; j++) if (!usedR.has(j)) {
    right[j].consensus = "single"; right[j].proposed_by = ["专家乙"];
    out.push(right[j]); log.singles.push({by:"乙", text:right[j].text, source:right[j].source_id});
  }
  return {claims:out, stats:{left_n:left.length, right_n:right.length,
    agreed_n:log.agreed.length, arbitrated_n:log.arbitrated.length,
    dropped_n:log.dropped.length, single_n:log.singles.length}, log};
}

/* ---------- 资源组装：对应 agents/generate.py ---------- */

function targetDifficulty(kpNode, m, lvlFn) {
  let base = kpNode.level;
  if (!m || m.status === "blind") base -= 1;
  else if (m.status === "strong") base += 1;
  base = Math.min(base, lvlFn(m) + 2);
  return Math.max(1, Math.min(5, base));
}

function learnerLevel(m, blindTh) {
  const sc = m ? m.score : 0;
  if (sc < blindTh) return 1;
  return Math.max(1, Math.min(5, 1 + Math.round((sc - blindTh) / (1 - blindTh) * 4)));
}

function assemble(kpNode, kp, claims, diff, R) {
  const cite = c => {
    const ch = R.get(c.source_id);
    return `${c.source_id}　${ch ? ch.source : ""}`;
  };
  const lecture = {
    kind:"lecture", kp, variant:"primary", difficulty:diff,
    title:`${kpNode.name}·要点讲义`, claims:claims.slice(),
    body:[`# ${kpNode.name}`, "", `难度 ${diff}/5，共 ${claims.length} 个要点。`, ""]
      .concat(claims.map((c,i) => `${i+1}. ${c.text}\n   > 依据：${cite(c)}`)).join("\n")
  };
  const sop = {
    kind:"sop", kp, variant:"primary", difficulty:diff,
    title:`${kpNode.name}·实操指南`, claims:claims.slice(),
    body:[`# ${kpNode.name}·实操指南`, "", "## 前置条件",
          "- 已完成开工前安全确认，围栏内无人",
          "- 示教器在手，模式旋钮处于手动并锁定", "", "## 操作步骤", ""]
      .concat(claims.map((c,i) => `**步骤 ${i+1}**　${c.text}\n　　依据 ${c.source_id}`))
      .concat(["", "## 常见错误", ""])
      .concat(claims.slice(0,2).map(c => `- 忽略「${c.text.slice(0,24)}…」这一条，容易在验收环节返工`))
      .join("\n")
  };
  /* 判断题必须有真有假。全是正命题等于没考 —— 一路点「正确」就是满分。
     假命题的"假"由审核闸认证：扰动后送回 auditOne，
     只有被判 contradicted 才采纳。与后端 _falsify 同一策略。 */
  const quizItems = claims.slice(0,3).map((c,i) => {
    const base = {
      stem:`关于${kpNode.name}，下列说法是否正确：${c.text}`,
      type:"judge", answer:true,
      difficulty:Math.max(1, Math.min(5, diff + (i-1))),
      source_id:c.source_id,
      explain:`依据${c.source_id}，该说法与知识库一致。`
    };
    if (i % 2 === 0) return base;
    const fake = falsify(c, R);
    if (!fake) return base;
    return Object.assign({}, base, {
      stem:`关于${kpNode.name}，下列说法是否正确：${fake}`,
      answer:false,
      explain:`依据${c.source_id}，原文并非如此；该说法已由审核环节判定与知识库冲突。`
    });
  });
  const quiz = {
    kind:"quiz", kp, variant:"primary", difficulty:diff,
    title:`${kpNode.name}·分阶测试题`, claims:claims.slice(),
    items:quizItems
  };
  return [lecture, sop, quiz];
}

/* 把真断言改成可认证的假命题。对应 agents/generate.py 的 _falsify。 */
function falsify(claim, R) {
  const nums = [...numbersIn(claim.text)];
  if (nums.length) {
    const target = nums.reduce((a,b) => parseFloat(b) > parseFloat(a) ? b : a);
    for (const f of [2, 3, 0.5, 4, 0.25]) {
      const v = parseFloat(target) * f;
      const nv = target.indexOf(".") >= 0
        ? String(parseFloat(v.toFixed(1))) : String(Math.round(v));
      if (nv === target) continue;
      const cand = claim.text.replace(target, nv);
      const r = auditOne({text:cand, source_id:claim.source_id}, R);
      if (r.verdict === "contradicted") return cand;
    }
  }
  return falsifyByTerm(claim, R);
}

function falsifyByTerm(claim, R) {
  if (!claim.source_id) return null;
  const cited = R.get(claim.source_id);
  if (!cited) return null;
  const dist = R.distinctive();
  const mine = [...new Set(tokenize(claim.text))].filter(t => dist.has(t));
  if (!mine.length) return null;
  const pool = [];
  for (const c of R.chunks) {
    if (c.kp === cited.kp) continue;
    for (const t of new Set(tokenize(c.title + " " + c.text)))
      if (dist.has(t) && mine.indexOf(t) < 0) pool.push(t);
    if (pool.length > 60) break;
  }
  for (const src of mine.sort()) {
    for (const repl of pool.slice(0, 40)) {
      const cand = claim.text.replace(src, repl);
      if (cand === claim.text) continue;
      const r = auditOne({text:cand, source_id:claim.source_id}, R);
      if (r.verdict === "contradicted") return cand;
    }
  }
  return null;
}

/* ---------- 编排：对应 orchestrator.py ---------- */

function runPipeline(diag, R, kpIndex, cfg, maxKp) {
  const events = [], resources = [], debates = [];
  let seq = 0;
  const push = (state, agent, summary, detail, ms) => {
    events.push({seq:++seq, state, agent, summary, detail:detail||{}, ms:ms||0});
  };

  push("DIAGNOSE", "学情诊断Agent",
       `完成自适应测评，识别盲区 ${diag.gaps.length} 个`,
       {overall:diag.overall, entry_level:diag.entry_level, gaps:diag.gaps});

  const path = diag.gaps.slice(0, maxKp || 4);
  push("PLAN", "编排层", `规划学习路径，本轮生成 ${path.length} 个知识点的资源`,
       {path, path_names:path.map(k => kpIndex[k].name)});

  for (const kp of path) {
    const node = kpIndex[kp];
    const m = diag.mastery.find(x => x.kp === kp);
    const lvlFn = mm => learnerLevel(mm, cfg.mastery_blind);
    const diff = targetDifficulty(node, m, lvlFn);

    let t0 = performance.now();
    const narrowHits = R.search(node.name, 3, kp);
    const wideTerms = [node.name].concat((node.prereq||[]).map(p => kpIndex[p] ? kpIndex[p].name : ""))
                                 .concat(node.tags || []).join(" ");
    let wideHits = R.search(wideTerms, 6).map(([c,s]) => [c, s * (c.kp === kp ? 1.5 : 0.7)]);
    wideHits.sort((a,b) => b[1]-a[1]);
    wideHits = wideHits.slice(0, 3);

    const narrowChunks = narrowHits.map(([c]) => c);
    const wideChunks = wideHits.map(([c]) => c);
    if (!narrowChunks.length && !wideChunks.length) continue;

    const left = draftClaims(narrowChunks, 5);
    const right = draftClaims(wideChunks, 5);
    push("GENERATE", "领域专家Agent 甲/乙",
         `「${node.name}」双专家独立起草：甲 ${left.length} 条（窄检索 ${narrowChunks.length} 片），乙 ${right.length} 条（宽检索 ${wideChunks.length} 片）`,
         {kp, chunks:narrowChunks.map(c=>c.id), wide_chunks:wideChunks.map(c=>c.id)},
         Math.round(performance.now()-t0));

    t0 = performance.now();
    const d = debate(left, right, R);
    debates.push({kp, stats:d.stats, log:d.log});
    push("DEBATE", "交叉验证裁判Agent",
         `交叉验证：印证 ${d.stats.agreed_n} 条，仲裁 ${d.stats.arbitrated_n} 条，存疑弃用 ${d.stats.dropped_n} 条，单方 ${d.stats.single_n} 条`,
         Object.assign({kp}, d), Math.round(performance.now()-t0));

    t0 = performance.now();
    const rv = review(d.claims, R);
    push("AUDIT", "审核裁判Agent",
         `逐条核验：通过 ${rv.kept.length} 条，拦截 ${rv.dropped.length} 条`,
         {kp, kept:rv.kept.length,
          dropped:rv.dropped.map(x => ({text:x.text, verdict:x.verdict, note:x.audit_note}))},
         Math.round(performance.now()-t0));

    const rs = assemble(node, kp, rv.kept, diff, R);
    for (const r of rs) r.dropped = rv.dropped;
    resources.push.apply(resources, rs);
    push("ASSEMBLE", "领域生成Agent",
         `「${node.name}」产出 ${rs.length} 种形态资源，难度 ${diff}/5`,
         {kp, difficulty:diff, kinds:rs.map(r=>r.kind)},
         Math.round(performance.now()-t0));
  }

  push("READY", "编排层", "首轮资源就绪，等待学习交互反馈",
       {resource_count:resources.length});

  const allClaims = resources.flatMap(r => r.claims);
  const droppedTexts = new Set(resources.flatMap(r => (r.dropped||[]).map(d => d.text)));
  return {
    diagnosis:diag, path, path_names:path.map(k => kpIndex[k].name),
    resources, events, debates, decisions:[],
    kp_index:kpIndex,
    metrics:{
      claims_kept:allClaims.length,
      claims_dropped:droppedTexts.size,
      gap_total:diag.gaps.length,
      gap_covered:diag.gaps.filter(g => resources.some(r => r.kp === g)).length,
      resource_count:resources.length,
      kinds:[...new Set(resources.map(r => r.kind))].sort()
    }
  };
}

/* ---------- 自述解析：对应 agents/intake.py 的规则通路 ---------- */

const EDU_PATS = [[/博士/, "博士"], [/硕士|研究生/, "硕士"],
  [/本科|大一|大二|大三|大四|学士/, "本科"], [/高职|大专|专科|高专/, "高职"],
  [/中职|技校|中专/, "中职"], [/高中/, "高中"]];
const GRADE_PATS = [[/大一|一年级/, "一年级"], [/大二|二年级/, "二年级"],
  [/大三|三年级/, "三年级"], [/大四|四年级/, "四年级"],
  [/在职|工作|上班|产线|车间/, "在职"]];
const MAJOR_PATS = [[/机械|机电|机制/, "机械类"], [/电气|自动化|电子/, "电气自动化类"],
  [/计算机|软件|信息/, "计算机类"], [/工业机器人|机器人/, "机器人技术"]];
const ZERO_HINTS = [/没(?:有)?(?:接触|碰|做|上手|操作)过/, /零基础/, /完全没/,
                    /第一次/, /从没/, /没干过/, /不会用/];
const HOUR_PATS = [[/(\d+(?:\.\d+)?)\s*(?:个)?小时/g, 1], [/(\d+(?:\.\d+)?)\s*天(?!级)/g, 8],
  [/(\d+(?:\.\d+)?)\s*周(?!级)/g, 40], [/(\d+(?:\.\d+)?)\s*(?:个)?月(?!级)/g, 160],
  [/(\d+(?:\.\d+)?)\s*年(?!级)/g, 1600]];
const CN_HOUR_PATS = [[/([一两二三四五六七八九十半])\s*年(?!级|段|检)/g, 1600],
  [/([一两二三四五六七八九十半])\s*(?:个)?月(?!级|段|检)/g, 160],
  [/([一两二三四五六七八九十半])\s*周(?!级|段|检)/g, 40],
  [/([一两二三四五六七八九十半])\s*天(?!级|段|检)/g, 8]];
const CN_SMALL = {"一":1,"两":2,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"半":0.5};

function parseIntake(text) {
  const t = String(text || "").trim();
  const out = {education:"", grade:"", major:"", hands_on_hours:0, goal:"", raw:t};
  if (!t) return out;
  for (const [re, v] of EDU_PATS) if (re.test(t)) { out.education = v; break; }
  for (const [re, v] of GRADE_PATS) if (re.test(t)) { out.grade = v; break; }
  for (const [re, v] of MAJOR_PATS) if (re.test(t)) { out.major = v; break; }

  let hours = 0, m;
  for (const [re, mul] of HOUR_PATS) { re.lastIndex = 0;
    while ((m = re.exec(t)) !== null) hours = Math.max(hours, parseFloat(m[1]) * mul); }
  for (const [re, mul] of CN_HOUR_PATS) { re.lastIndex = 0;
    while ((m = re.exec(t)) !== null) hours = Math.max(hours, (CN_SMALL[m[1]]||0) * mul); }
  if (ZERO_HINTS.some(re => re.test(t))) hours = 0;
  out.hands_on_hours = Math.floor(Math.min(hours, 2000));

  const g = t.match(/(?:想|希望|打算|目标是|准备)([^。；\n]{2,40})/);
  if (g) out.goal = g[1].trim();
  return out;
}

function clarify(bg) {
  const qs = [];
  if (!bg.education) qs.push("你目前的学历或在读层次是？");
  if (!bg.hands_on_hours) qs.push("你实际上手操作过工业机器人吗？大概多久？");
  if (!bg.major && !bg.grade) qs.push("你的专业方向或者现在的岗位是什么？");
  return qs.slice(0, 2);
}

global.Engine = {
  tokenize, overlapRatio, jaccard, numbersIn, cnToInt,
  Retriever, BKT, Adaptive, debate, review, auditOne, semanticBoundaryIssue,
  luckProbability, masteryInterval, evidenceState, buildAbility,
  makeItem, vetItem, quantities, isNumericOption,
  runPipeline, parseIntake, clarify, learnerLevel, splitSentences, draftClaims
};

})(window);
