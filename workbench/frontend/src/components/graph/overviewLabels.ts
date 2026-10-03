// 全部项目星图的名字：断行、名字预算和避让。纯函数，输入是这一帧每颗行星的屏幕位置和量好的标签尺寸。
//
// 名字预算：全图大约 40 个，优先级是有琥珀数 > 窗口内场次多 > 越近开过会；超出预算的只画本体、悬停才出名字。
// 滚轮放大时预算按 40×缩放^2.2 增长，同一优先级逐步放出；放大到 1.35× 以上，小行星带里的名字才开始放。
// 预算是上限：放不下（会压到别的字、圈名、琥珀数，或出舞台）的就不显示，所以任何缩放下字都不互压。
// 避让是每帧一次：贪心挑候选位置 → 修补两轮 → 按优先级逐个挪最小的一步消重叠 → 还压着的不显示；
// 上一帧选的位置略占便宜（迟滞），拖动转台时标签不来回翻边。

export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** 两个框重叠的面积（不重叠为 0） */
export function rectOverlap(a: Rect, b: Rect): number {
  return Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
}

export const NAME_BUDGET = 40;
/** 放大到这个倍数以上，小行星带里的名字才开始按预算放出来 */
export const BELT_NAME_ZOOM = 1.35;
/** 「在图上找」命中的前这么多个强制写名字 */
export const FIND_NAMED_MAX = 12;

export function nameBudget(zoom: number): number {
  return Math.round(NAME_BUDGET * Math.pow(zoom, 2.2));
}

// ------------------------------------------------------------------ 断行

// 名字最多两行。优先断在常见的尾词前、西文词后，不拆开这些词（取自现有项目名里最常见的搭配）
const TAILS = ["新项目", "项目", "方案", "配置", "平台", "整合", "迁移", "口径", "交接", "开通", "管理", "同行", "二类证", "科普", "患者", "绑定", "结算", "上线", "账号", "名单", "领取", "资产"];
const NOSPLIT = [...TAILS, "小程序", "基金会", "供应商", "口服药", "云讲堂", "质检", "录音", "复制", "一键", "转移", "到店", "信号", "疗法", "医生", "随访", "西苑", "北京"];
/** 一行最多约 7.5 个汉字宽 */
const LINE_UNITS = 7.4;

/** 估一个字的宽度（以一个汉字为 1） */
function unitWidth(ch: string): number {
  if (/[⺀-鿿＀-￯　-〿]/.test(ch)) return 1;
  if (/[A-Z]/.test(ch)) return 0.64;
  if (/\s/.test(ch)) return 0.3;
  return 0.55;
}

function textUnits(text: string): number {
  let sum = 0;
  for (const ch of text) sum += unitWidth(ch);
  return sum;
}

/** 名字断成一到两行：两行差不多长，不拆西文词和常见搭配 */
export function breakName(name: string): string[] {
  if (textUnits(name) <= LINE_UNITS) return [name];
  const tokens = name.match(/[A-Za-z0-9]+|\s+|./gu) ?? [name];
  let best: string[] = [name];
  let bestScore = Number.NEGATIVE_INFINITY;
  for (let index = 1; index < tokens.length; index += 1) {
    const head = tokens.slice(0, index).join("").trim();
    const tail = tokens.slice(index).join("").trim();
    if (!head || !tail) continue;
    const headUnits = textUnits(head);
    const tailUnits = textUnits(tail);
    let score = -Math.abs(headUnits - tailUnits) * 0.6;
    if (headUnits > LINE_UNITS + 0.2 || tailUnits > LINE_UNITS + 0.2) score -= 10;
    if (/^[A-Za-z0-9]{2,}$/.test(tokens[index - 1])) score += 3;
    if (TAILS.some((word) => tail.startsWith(word))) score += 2;
    const cut = head.length;
    for (const word of NOSPLIT) {
      let at = name.indexOf(word);
      while (at >= 0) {
        if (cut > at && cut < at + word.length) score -= 4;
        at = name.indexOf(word, at + 1);
      }
    }
    if (score > bestScore) {
      bestScore = score;
      best = [head, tail];
    }
  }
  return best;
}

// ------------------------------------------------------------------ 避让

export type LabelAlign = "start" | "end" | "center";

/** 一颗行星在这一帧里和名字有关的一切 */
export interface LabelBody {
  id: string;
  /** 本体中心（屏幕像素） */
  x: number;
  y: number;
  /** 本体外半径：标签、命中、琥珀数都按它 */
  rs: number;
  /** 前后坐标（> 0 在太阳前面） */
  zr: number;
  ring: number;
  /** 这一圈在这个深度上的屏幕半径：判断行星在不在圈的左右两侧 */
  ringPx: number;
  belt: boolean;
  waiting: number;
  meetings: number;
  age: number | null;
  /** 量好的标签尺寸；没量到（舞台还没大小）时是 0 */
  w: number;
  h: number;
  /** 琥珀数的宽，没有琥珀数为 0 */
  badgeW: number;
}

/** 圈名：默认位置，和能不能看见（转到太阳背后时藏起来） */
export interface ChipSlot {
  rect: Rect;
  hidden: boolean;
}

export interface LabelInput {
  width: number;
  height: number;
  /** 右边被面板盖住的宽度：名字不往面板底下放（放不下的就不写） */
  rightInset?: number;
  /** 太阳光点的屏幕位置，gx 是盘面中心 */
  sun: { x: number; y: number; gx: number };
  sunLabel: { w: number; h: number };
  /** HUD（读数块、搜索框、缩放条）占的地方，已经留好边 */
  hud: Rect[];
  bodies: LabelBody[];
  chips: ChipSlot[];
  /** 第 k 圈的圈名沿轨道挪 off 度以后的框 */
  chipAt: (k: number, off: number) => Rect;
  /** 第 k 圈的圈名最多挪多少度（不挪出空档） */
  chipSpan: (k: number) => number;
  zoom: number;
  /** 「在图上找」命中的（按名次），最先放、一定写名字 */
  found: string[];
  /** 悬停或在面板里指着的：预算外也写名字 */
  reveal: Set<string>;
  /** 上一帧每个标签选的位置 */
  lastKey: Map<string, string>;
  /** 飞入时不写名字 */
  silent: boolean;
  /**
   * 指针停在名字上时，这个名字钉在原地：行星悬停时会抬起，名字要是跟着挪，就从指针底下溜走、悬停跟着丢了
   */
  pinned?: { id: string; spot: PlacedLabel } | null;
}

export interface PlacedLabel {
  x0: number;
  y0: number;
  align: LabelAlign;
  key: string;
  rect: Rect;
}

export interface LabelOutput {
  labels: Map<string, PlacedLabel>;
  /** 标签离自己的行星太远时画的细引线：从本体边到标签上最近的点 */
  leaders: Array<{ x: number; y: number; nx: number; ny: number; r: number }>;
  sunLabel: { x0: number; y0: number; end: boolean };
  chips: Array<{ x0: number; y0: number }>;
  /** 琥珀数的左上角 */
  badges: Map<string, { x: number; y: number }>;
}

interface Obstacle extends Rect {
  w: number;
  id?: string;
}

interface Candidate {
  key: string;
  index: number;
  x0: number;
  y0: number;
  align: LabelAlign;
}

const BOUNDS_PAD = 6;
const LEADER_GAP = 12;

/** 琥珀数挂在本体右上角 */
export function badgeAt(body: Pick<LabelBody, "x" | "y" | "rs">): { x: number; y: number } {
  return { x: body.x + body.rs * 0.62 - 4, y: body.y - body.rs * 0.62 - 13 };
}

const priority = (a: LabelBody, b: LabelBody) =>
  Number(b.waiting > 0) - Number(a.waiting > 0) ||
  b.meetings - a.meetings ||
  (a.age ?? 1e9) - (b.age ?? 1e9) ||
  b.zr - a.zr;

/** 候选位置：左右两侧的写在旁边，前排写在下面，最外圈后排写在上面，其余写在朝外那一侧 */
function candidates(body: LabelBody, sunGx: number): Candidate[] {
  const { x, y, w, h } = body;
  const r = body.rs;
  const g = 5;
  const spot: Record<string, [number, number, LabelAlign]> = {
    R: [x + r + g, y - h / 2, "start"],
    L: [x - r - g - w, y - h / 2, "end"],
    B: [x - w / 2, y + r + 3, "center"],
    T: [x - w / 2, y - r - 3 - h - (body.badgeW ? 9 : 0), "center"],
    TR: [x + r * 0.55 + 3, y - r * 0.55 - h, "start"],
    TL: [x - r * 0.55 - 3 - w, y - r * 0.55 - h, "end"],
    BR: [x + r * 0.55 + 3, y + r * 0.55, "start"],
    BL: [x - r * 0.55 - 3 - w, y + r * 0.55, "end"],
    Ru: [x + r + g, y - h + 7, "start"],
    Rd: [x + r + g, y - 7, "start"],
    Lu: [x - r - g - w, y - h + 7, "end"],
    Ld: [x - r - g - w, y - 7, "end"],
  };
  const xr = x - sunGx;
  const side = Math.abs(xr) / Math.max(1, body.ringPx);
  const right = xr >= 0;
  let order: string[];
  if (side > 0.55) order = right ? ["R", "Ru", "Rd", "BR", "TR", "B", "T", "L"] : ["L", "Lu", "Ld", "BL", "TL", "B", "T", "R"];
  else if (body.zr > 0) order = right ? ["B", "BR", "R", "Rd", "BL", "L", "T"] : ["B", "BL", "L", "Ld", "BR", "R", "T"];
  else if (body.ring === 2) order = right ? ["T", "TR", "R", "Ru", "TL", "L", "B"] : ["T", "TL", "L", "Lu", "TR", "R", "B"];
  else order = right ? ["R", "TR", "Ru", "T", "Rd", "L", "B"] : ["L", "TL", "Lu", "T", "Ld", "R", "B"];
  return order.map((key, index) => ({ key, index, x0: spot[key][0], y0: spot[key][1], align: spot[key][2] }));
}

const pad = (r: Rect, dx: number, dy: number): Rect => ({ x0: r.x0 - dx, y0: r.y0 - dy, x1: r.x1 + dx, y1: r.y1 + dy });
const around = (body: LabelBody, extra: number): Rect => ({
  x0: body.x - body.rs - extra,
  y0: body.y - body.rs - extra,
  x1: body.x + body.rs + extra,
  y1: body.y + body.rs + extra,
});

export function placeLabels(input: LabelInput): LabelOutput {
  const { width, height, sun, bodies } = input;
  const bounds = { x0: BOUNDS_PAD, y0: BOUNDS_PAD, x1: width - (input.rightInset ?? 0) - BOUNDS_PAD, y1: height - BOUNDS_PAD };
  const inBounds = (r: Rect) => r.x0 >= bounds.x0 && r.x1 <= bounds.x1 && r.y0 >= bounds.y0 && r.y1 <= bounds.y1;

  const obstacles: Obstacle[] = input.hud.map((rect) => ({ ...rect, w: 1 }));
  obstacles.push({ x0: sun.x - 18, y0: sun.y - 18, x1: sun.x + 18, y1: sun.y + 18, w: 1.2 });
  for (const body of bodies) {
    const box: Obstacle = { ...around(body, 3), id: body.id, w: 1.6 };
    if (body.badgeW) {
      const badge = badgeAt(body);
      box.x1 = Math.max(box.x1, badge.x + body.badgeW + 2);
      box.y0 = Math.min(box.y0, badge.y - 2);
    }
    obstacles.push(box);
  }
  // 圈名、琥珀数、太阳的字、读数块和搜索框是「硬」障碍：名字最后还压着它们的就不显示
  const hard: Rect[] = [...input.hud];
  for (const chip of input.chips) {
    if (chip.hidden) continue;
    const rect = pad(chip.rect, 3, 2);
    obstacles.push({ ...rect, w: 3 });
    hard.push(rect);
  }

  // 太阳的字放在它旁边（像星图里给亮星注名），先于行星的名字定位
  const { w: sw, h: sh } = input.sunLabel;
  const sunSpots: Array<[number, number, boolean]> = [
    [sun.x + 20, sun.y - sh - 6, false],
    [sun.x - 20 - sw, sun.y - sh - 6, true],
    [sun.x + 22, sun.y + 8, false],
    [sun.x - 22 - sw, sun.y + 8, true],
    [sun.x + 26, sun.y - sh / 2, false],
  ];
  let sunBest: { cost: number; rect: Rect; end: boolean } | null = null;
  for (const [x0, y0, end] of sunSpots) {
    const rect = { x0, y0, x1: x0 + sw, y1: y0 + sh };
    let cost = 0;
    for (const o of obstacles) cost += rectOverlap(rect, o) * o.w;
    for (const body of bodies) cost += rectOverlap(rect, around(body, 3)) * 2;
    if (!sunBest || cost < sunBest.cost) sunBest = { cost, rect, end };
  }
  const sunRect = sunBest!.rect;
  obstacles.push({ ...pad(sunRect, 3, 0), w: 3 });
  hard.push(pad(sunRect, 2, 1));

  // 名字预算：找到的先放，再按优先级放到预算用完；预算外的只有悬停、面板里指着的才写
  const found = new Set(input.found.slice(0, FIND_NAMED_MAX));
  const budget = nameBudget(input.zoom);
  const beltNames = input.zoom >= BELT_NAME_ZOOM;
  const named: LabelBody[] = [];
  const extra: LabelBody[] = [];
  if (!input.silent) {
    let used = 0;
    for (const body of [...bodies].sort(priority)) {
      if (found.has(body.id)) extra.push(body);
      else if (used < budget && (!body.belt || beltNames)) {
        named.push(body);
        used += 1;
      } else if (input.reveal.has(body.id)) extra.push(body);
    }
  }
  const pinned = input.pinned && !input.silent ? input.pinned : null;
  const pinnedBody = pinned ? bodies.find((body) => body.id === pinned.id) : undefined;
  // 钉住的排第一个：收尾挪位置时它不动，别人给它让
  const order = [
    ...(pinnedBody ? [pinnedBody] : []),
    ...[...extra, ...named].filter((body) => body !== pinnedBody),
  ];
  const forced = new Set(extra.map((body) => body.id));
  if (pinnedBody) forced.add(pinnedBody.id);

  const costOf = (body: LabelBody, c: Candidate, placed: Map<string, Rect>) => {
    const rect = { x0: c.x0, y0: c.y0, x1: c.x0 + body.w, y1: c.y0 + body.h };
    let cost = c.index * 0.6 - (c.key === input.lastKey.get(body.id) ? 1.1 : 0);
    cost +=
      (Math.max(0, bounds.x0 - rect.x0) +
        Math.max(0, rect.x1 - bounds.x1) +
        Math.max(0, bounds.y0 - rect.y0) +
        Math.max(0, rect.y1 - bounds.y1)) *
      400;
    for (const o of obstacles) if (o.id !== body.id) cost += rectOverlap(rect, o) * o.w;
    for (const [id, o] of placed) if (id !== body.id) cost += rectOverlap(rect, o) * 3;
    // 贴着别人的行星也算一点代价，免得名字跟错星
    for (const other of bodies) {
      if (other === body) continue;
      const near = rectOverlap(rect, around(other, 9));
      if (near) cost += 0.8 + near * 0.05;
    }
    return { cost, rect, c };
  };
  type Choice = ReturnType<typeof costOf>;
  const placed = new Map<string, Rect>();
  const choice = new Map<string, Choice>();
  const pickBest = (body: LabelBody) => {
    let best: Choice | null = null;
    for (const c of candidates(body, sun.gx)) {
      const option = costOf(body, c, placed);
      if (!best || option.cost < best.cost) best = option;
    }
    return best!;
  };
  for (const body of order) {
    const best =
      body === pinnedBody && pinned
        ? {
            cost: 0,
            rect: pinned.spot.rect,
            c: { key: pinned.spot.key, index: 0, x0: pinned.spot.x0, y0: pinned.spot.y0, align: pinned.spot.align },
          }
        : pickBest(body);
    choice.set(body.id, best);
    placed.set(body.id, pad(best.rect, 2, 1));
  }
  // 修补：仍压着别人的，在所有人都放好之后再挑一次
  for (let pass = 0; pass < 2; pass += 1) {
    for (const body of order) {
      const current = choice.get(body.id)!;
      if (body === pinnedBody || current.cost <= current.c.index * 0.6) continue;
      placed.delete(body.id);
      const best = pickBest(body);
      const keep = best.cost < current.cost ? best : current;
      choice.set(body.id, keep);
      placed.set(body.id, pad(keep.rect, 2, 1));
    }
  }

  // 收尾：按优先级逐个检查，还压着更靠前的名字、别人的行星、圈名或琥珀数的，往上下左右挪最小的一步让开
  const badges = new Map<string, { x: number; y: number }>();
  for (const body of bodies) {
    if (!body.badgeW) continue;
    const badge = badgeAt(body);
    badges.set(body.id, badge);
    if (!input.silent) hard.push({ x0: badge.x - 1, y0: badge.y - 1, x1: badge.x + body.badgeW + 1, y1: badge.y + 19 });
  }
  const final = order.map((body) => ({ body, rect: { ...choice.get(body.id)!.rect } }));
  for (let j = 1; j < final.length; j += 1) {
    const me = final[j];
    const blockers: Rect[] = [
      ...final.slice(0, j).map((item) => item.rect),
      ...hard,
      ...bodies.filter((other) => other !== me.body).map((other) => around(other, 0)),
    ];
    const hits = (r: Rect) => {
      const grown = pad(r, 1, 1);
      let sum = 0;
      for (const o of blockers) sum += rectOverlap(grown, o);
      return sum;
    };
    if (!hits(me.rect)) continue;
    let moved: Rect | null = null;
    for (let step = 2; step <= 64 && !moved; step += 2) {
      for (const [dx, dy] of [
        [0, -step],
        [0, step],
        [-step, 0],
        [step, 0],
      ]) {
        const r = { x0: me.rect.x0 + dx, x1: me.rect.x1 + dx, y0: me.rect.y0 + dy, y1: me.rect.y1 + dy };
        if (inBounds(r) && !hits(r)) {
          moved = r;
          break;
        }
      }
    }
    if (moved) me.rect = moved;
  }

  // 还是放不下的就不显示（预算是上限，不保证每个都有位置；找到的、悬停的除外）
  const labels = new Map<string, PlacedLabel>();
  const leaders: LabelOutput["leaders"] = [];
  const shown: Rect[] = [];
  for (const { body, rect } of final) {
    const bad = !inBounds(rect) || shown.some((o) => rectOverlap(rect, o) > 0) || hard.some((o) => rectOverlap(rect, o) > 0);
    if (bad && !forced.has(body.id)) continue;
    shown.push(rect);
    const c = choice.get(body.id)!.c;
    labels.set(body.id, { x0: rect.x0, y0: rect.y0, align: c.align, key: c.key, rect });
    const nx = Math.max(rect.x0, Math.min(body.x, rect.x1));
    const ny = Math.max(rect.y0, Math.min(body.y, rect.y1));
    if (Math.hypot(nx - body.x, ny - body.y) - body.rs > LEADER_GAP) leaders.push({ x: body.x, y: body.y, nx, ny, r: body.rs });
  }

  // 圈名压到名字时，在空档里左右滑一点：压到字比压到行星严重得多
  const chips = input.chips.map((chip, k) => {
    let best = chip.rect;
    const hit = (r: Rect) => {
      const grown = pad(r, 6, 3);
      let sum = 0;
      for (const o of shown) sum += rectOverlap(r, o) * 50 + rectOverlap(grown, o);
      for (const body of bodies) sum += rectOverlap(grown, around(body, 2));
      return sum;
    };
    if (!chip.hidden) {
      let bestHit = hit(best);
      const span = input.chipSpan(k);
      for (let off = 2; off <= span && bestHit > 0; off += 2) {
        for (const sign of [1, -1]) {
          const r = input.chipAt(k, sign * off);
          const h = hit(r);
          if (h < bestHit) {
            bestHit = h;
            best = r;
          }
        }
      }
    }
    return { x0: best.x0, y0: best.y0 };
  });

  return { labels, leaders, sunLabel: { x0: sunRect.x0, y0: sunRect.y0, end: sunBest!.end }, chips, badges };
}
