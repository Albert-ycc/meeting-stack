/*
 * 需求详情「出自录音」里，波形上方的时间签怎么摆。
 *
 * 签贴着自己的锚点：锚点在录音的 p%（0～100）处，签的左沿取 (波形宽 − 签宽) × p，
 * 也就是 p=0 时签的左沿对齐波形左边、p=100 时右沿对齐波形右边、p=50 时正好居中；
 * 锚点到了录音头尾，签也不会再伸出卡片的边框（原来一律居中，伸出去 20 多像素）。
 * 锚点挨得很近的（比如 22:20 和 22:25 在一小时的录音上只差 1 像素多）几个签会叠在一起，
 * 这里把它们错开成几行，同一行里签与签之间留一道缝，每个都读得出来。
 */

/** 一行签的占位高度：签本身 22px，加 4px 行距 */
export const FLAG_ROW_HEIGHT = 26;

export interface FlagBox {
  id: number;
  /** 锚点在录音里的位置，0～100 */
  percent: number;
  /** 签的实际宽度（像素，量出来的，不估） */
  width: number;
}

export interface FlagLayout {
  /** 一共几行，至少 1 */
  rows: number;
  /** 每个签在第几行：0 是最靠近波形的一行，往上递增 */
  rowOf: Map<number, number>;
}

export const EMPTY_FLAG_LAYOUT: FlagLayout = { rows: 1, rowOf: new Map() };

/** 签的左沿（像素） */
export function flagLeft(box: FlagBox, containerWidth: number): number {
  return ((containerWidth - box.width) * box.percent) / 100;
}

/**
 * 从左到右一个个放：放进第一行放得下的，放不下（和那一行最右边的签挨着或叠着）就换到上一行。
 * 容器宽度还量不出来（0，比如刚挂上、或没有布局的环境）时不排，全放第一行。
 */
export function packFlags(boxes: FlagBox[], containerWidth: number, gap = 6): FlagLayout {
  if (containerWidth <= 0 || boxes.length === 0) return EMPTY_FLAG_LAYOUT;
  const rowOf = new Map<number, number>();
  // 每一行目前最靠右的右沿
  const rightEdges: number[] = [];
  const ordered = [...boxes].sort((first, second) => first.percent - second.percent);
  for (const box of ordered) {
    const left = flagLeft(box, containerWidth);
    let row = rightEdges.findIndex((edge) => left >= edge + gap);
    if (row < 0) {
      row = rightEdges.length;
      rightEdges.push(left + box.width);
    } else {
      rightEdges[row] = Math.max(rightEdges[row], left + box.width);
    }
    rowOf.set(box.id, row);
  }
  return { rows: Math.max(1, rightEdges.length), rowOf };
}

export function sameFlagLayout(first: FlagLayout, second: FlagLayout): boolean {
  if (first.rows !== second.rows || first.rowOf.size !== second.rowOf.size) return false;
  for (const [id, row] of first.rowOf) {
    if (second.rowOf.get(id) !== row) return false;
  }
  return true;
}
