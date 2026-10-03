import { describe, expect, it } from "vitest";

/*
 * pageStyles.ts 是各页拆块以后样式的加载顺序（见那份文件的开头）。它是手写的清单：
 * 谁新加了样式文件却没写进去，样式会跟着自己的页面代码晚到，层叠顺序就和别的页不一样了。这里拦住漏加的。
 */
const SOURCES = import.meta.glob(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const STYLESHEETS = Object.keys(import.meta.glob("./**/*.css"));

// 没有任何页面用到的组件：整包里本来就没有它的样式，清单里也不放。有人用上它时下面第四条会红
const UNUSED_COMPONENT = "./components/graph/ViewModeToggle";

const CSS_IMPORT = /^\s*import\s+["'](\.{1,2}\/[^"']+\.css)["'];?\s*$/gm;

/** 把源码里相对路径的样式引用，换成相对 src 的 ./components/X.css 这种写法 */
function resolve(fromFile: string, specifier: string): string {
  const parts = fromFile.split("/").slice(0, -1);
  for (const segment of specifier.split("/")) {
    if (segment === ".") continue;
    if (segment === "..") parts.pop();
    else parts.push(segment);
  }
  return parts.join("/");
}

function cssImports(file: string): string[] {
  return [...SOURCES[file].matchAll(CSS_IMPORT)].map((match) => resolve(file, match[1]));
}

const manifest = cssImports("./pageStyles.ts");

describe("pageStyles 清单", () => {
  it("代码里 import 的每个样式文件都在清单里", () => {
    const used = new Set(
      Object.keys(SOURCES)
        .filter((file) => file !== "./pageStyles.ts" && !file.startsWith(`${UNUSED_COMPONENT}.`))
        .flatMap(cssImports),
    );
    const missing = [...used].filter((css) => !manifest.includes(css)).sort();
    expect(missing, "这些样式文件被代码引用了，却没写进 pageStyles.ts").toEqual([]);
  });

  it("清单里没有重复的、也没有指向不存在的文件", () => {
    expect(manifest.length).toBeGreaterThan(0);
    expect(manifest.filter((css, index) => manifest.indexOf(css) !== index)).toEqual([]);
    expect(manifest.filter((css) => !STYLESHEETS.includes(css))).toEqual([]);
  });

  it("styles.css 排在最后：全局样式压在各页样式后面，和拆块以前一样", () => {
    expect(manifest.at(-1)).toBe("./styles.css");
  });

  it("没人用的组件还是没人用；有人用上它，就要把它的样式文件加进清单、再删掉上面的例外", () => {
    const name = UNUSED_COMPONENT.split("/").at(-1);
    const users = Object.entries(SOURCES)
      .filter(([file, text]) => !file.startsWith(`${UNUSED_COMPONENT}.`) && new RegExp(`from\\s+["'][^"']*/${name}["']`).test(text))
      .map(([file]) => file);
    expect(users).toEqual([]);
  });
});
