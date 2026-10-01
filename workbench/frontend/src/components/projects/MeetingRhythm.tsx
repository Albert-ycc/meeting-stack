import "./MeetingRhythm.css";

const WEEKS = 12;

interface MeetingRhythmProps {
  /** 近 12 周每周的会议场数，最后一格是本周；旧后端没有这个字段时不画 */
  weeks: number[] | undefined;
}

/** 取最后 12 格，不足的在最前面补 0：格数固定，卡片之间才对得齐 */
function normalize(weeks: number[]): number[] {
  const tail = weeks.slice(-WEEKS);
  return [...Array<number>(WEEKS - tail.length).fill(0), ...tail];
}

function barTitle(weeksAgo: number, count: number) {
  return weeksAgo === 0 ? `本周 · ${count} 场` : `${weeksAgo} 周前 · ${count} 场`;
}

/** 近 12 周会议节奏：每周一根柱子，本周单独标色，0 场画成短横线 */
export function MeetingRhythm({ weeks }: MeetingRhythmProps) {
  if (!weeks) return <div className="rhythm rhythm--empty" />;
  const counts = normalize(weeks);
  const total = counts.reduce((sum, count) => sum + count, 0);
  // 单周最多时撑满；整体很稀疏时不让 1 场的柱子也顶到头
  const scale = Math.max(3, ...counts);
  return (
    <div className="rhythm">
      <div className="rhythm__head">
        <span>会议节奏</span>
        <span>近 12 周 {total} 场</span>
      </div>
      <div aria-label="近 12 周会议节奏" className="rhythm__bars" role="img">
        {counts.map((count, index) => {
          const weeksAgo = WEEKS - 1 - index;
          return (
            <span
              className={`rhythm__bar ${count === 0 ? "is-zero" : ""} ${weeksAgo === 0 ? "is-now" : ""}`}
              data-count={count}
              key={weeksAgo}
              style={count === 0 ? undefined : { height: `${Math.max(4, (count / scale) * 100)}%` }}
              title={barTitle(weeksAgo, count)}
            />
          );
        })}
      </div>
      <div className="rhythm__axis">
        <span>12 周前</span>
        <span>本周</span>
      </div>
    </div>
  );
}
