import { describe, expect, it } from "vitest";

/*
 * 两套主题里文字的对比度（WCAG：小字 4.5:1）。真浏览器里 e32 扫出来、低于 3:1 的几处都落在这三类上：
 * --faint 当文字用（表头、图表说明、空态、「没有来源录音」）、橙底上的字、写死的绿字。
 * 这里按 styles.css 里 token 的真实取值算数，再查所有 CSS 里橙底的字是不是都走 --on-signal，
 * 以后谁加了白字橙底的按钮、或者把 token 调回去，用例会红。
 */
const STYLESHEETS = import.meta.glob(["./styles.css", "./components/**/*.css"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

type Theme = "dark" | "light";
type Rgb = [number, number, number];
interface Rgba {
  rgb: Rgb;
  alpha: number;
}

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** 取一个 token 块里 `--名字: 值;` 的全部声明 */
function declarations(block: string): Record<string, string> {
  const found: Record<string, string> = {};
  for (const match of block.matchAll(/(--[\w-]+):\s*([^;]+);/g)) found[match[1]] = match[2].trim();
  return found;
}

function tokens(theme: Theme): Record<string, string> {
  const css = stripComments(STYLESHEETS["./styles.css"]);
  const dark = declarations(css.match(/:root\s*\{([^}]*)\}/)![1]);
  if (theme === "dark") return dark;
  // 浅色主题没覆盖的 token（比如 --on-signal）沿用 :root 里的
  return { ...dark, ...declarations(css.match(/:root\[data-theme="light"\]\s*\{([^}]*)\}/)![1]) };
}

function parseColor(value: string): Rgba {
  const hex = value.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return { rgb: [(n >> 16) & 255, (n >> 8) & 255, n & 255], alpha: 1 };
  }
  const fn = value.match(/^rgba?\(([^)]+)\)$/);
  if (!fn) throw new Error(`认不出的颜色：${value}`);
  const [r, g, b, a] = fn[1].split(/[ ,/]+/).filter(Boolean).map(Number);
  return { rgb: [r, g, b], alpha: a ?? 1 };
}

function over(top: Rgba, base: Rgb): Rgb {
  return base.map((channel, index) => top.rgb[index] * top.alpha + channel * (1 - top.alpha)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const solid = (theme: Theme, name: string): Rgb => {
  const color = parseColor(tokens(theme)[name]);
  if (color.alpha !== 1) throw new Error(`${name} 不是实色`);
  return color.rgb;
};

/** 文字会落在的底：页面底、卡片、凸起面，以及卡片上再叠一层 --line 的小标签 */
function backgrounds(theme: Theme): Array<[string, Rgb]> {
  const surface = solid(theme, "--surface");
  const line = parseColor(tokens(theme)["--line"]);
  return [
    ["--bg", solid(theme, "--bg")],
    ["--surface", surface],
    ["--surface-2", solid(theme, "--surface-2")],
    ["--stage", solid(theme, "--stage")],
    ["--raised", solid(theme, "--raised")],
    ["--surface 上的 --line 标签", over(line, surface)],
  ];
}

const THEMES: Theme[] = ["dark", "light"];

describe.each(THEMES)("%s 主题的文字对比度", (theme) => {
  it("--faint 当小字用（表头、图表说明、空态、没有来源录音）：落在任何一层底上都 ≥ 4.5:1", () => {
    const faint = solid(theme, "--faint");
    for (const [name, background] of backgrounds(theme)) {
      expect(ratio(faint, background), `--faint 在 ${name} 上`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("橙底上的字（--on-signal）压在 --signal 上 ≥ 4.5:1", () => {
    expect(ratio(solid(theme, "--on-signal"), solid(theme, "--signal"))).toBeGreaterThanOrEqual(4.5);
  });

  it("在等你的数（--warn-strong 底）：深色配 --on-signal、浅色配白字 ≥ 4.5:1；琥珀小字落在舞台和凸起面上 ≥ 4.5:1", () => {
    const amber = solid(theme, "--warn-strong");
    const text = solid(theme, theme === "dark" ? "--on-signal" : "--surface");
    expect(ratio(text, amber)).toBeGreaterThanOrEqual(4.5);
    for (const name of ["--stage", "--raised"]) {
      expect(ratio(amber, solid(theme, name)), `--warn-strong 在 ${name} 上`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("绿字（--ok-strong）落在页面底、卡片和它自己的淡绿底上都 ≥ 4.5:1", () => {
    const okStrong = solid(theme, "--ok-strong");
    const okBg = parseColor(tokens(theme)["--ok-bg"]);
    for (const [name, background] of backgrounds(theme).slice(0, 2)) {
      expect(ratio(okStrong, background), `--ok-strong 在 ${name} 上`).toBeGreaterThanOrEqual(4.5);
      expect(ratio(okStrong, over(okBg, background)), `--ok-strong 在 ${name} 上的淡绿底上`).toBeGreaterThanOrEqual(4.5);
    }
  });
});

it("深色主题里悬停时橙底变亮（--signal-dark），橙底上的字照样 ≥ 4.5:1", () => {
  expect(ratio(solid("dark", "--on-signal"), solid("dark", "--signal-dark"))).toBeGreaterThanOrEqual(4.5);
});

describe("橙底的字一律走 --on-signal", () => {
  const SOLID_ORANGE = /(?:^|[;\s])background(?:-color)?:\s*var\(--signal(?:-dark)?\)\s*(?:;|$)/;
  const TEXT_COLOR = /(?:^|[;\s])color:\s*([^;]+?)\s*(?:;|$)/;

  it("所有 CSS 里，实色橙底（--signal、--signal-dark）的规则要是写了字色，只能是 var(--on-signal)", () => {
    const offenders: string[] = [];
    for (const [file, raw] of Object.entries(STYLESHEETS)) {
      for (const rule of stripComments(raw).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const body = rule[2];
        const color = body.match(TEXT_COLOR)?.[1];
        if (SOLID_ORANGE.test(body) && color && color !== "var(--on-signal)") {
          offenders.push(`${file}  ${rule[1].trim().replace(/\s+/g, " ")}  color: ${color}`);
        }
      }
    }
    // 深色主题里 --ink、#fff 压在 #f0783b 上只有 2.4~2.8:1，原来这样写的有 50 来处
    expect(offenders).toEqual([]);
  });
});
