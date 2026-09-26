import { useState } from "react";

import type { ApiClient } from "../api";
import { isComposingKeydown } from "../keyboard";
import type { ProjectAlsoName, ProjectRecognitionProfile } from "../types";
import "./ProjectRecognitionCard.css";

interface ProjectRecognitionCardProps {
  apiClient: ApiClient;
  projectId: string;
  projectName: string;
  profile: ProjectRecognitionProfile;
  canWrite: boolean;
  /** 叫法改完后重新读项目 */
  onChanged: () => void | Promise<void>;
}

const SOURCE_NOTES: Record<ProjectAlsoName["source"], string> = {
  manual: "",
  former: "曾用名",
  merged: "合并来的",
};

const LENGTH_MESSAGE = "叫法要 2–20 个字，不能是纯数字";
const SHORT_WARNING = "两个字的叫法容易撞车，只有在一场会里出现 2 次以上才算数";

/** 和后端 validate_also_name 同一套规则，先在前端就地提示；泛词、撞别的项目交给后端。 */
function alsoNameProblem(text: string): string {
  const length = Array.from(text).length;
  if (length < 2 || length > 20 || /^\d+$/.test(text) || !/\p{L}/u.test(text)) return LENGTH_MESSAGE;
  return "";
}

/** 项目详情里「系统怎么认出这个项目」：名称、也叫、文件夹名、项目词和最近 30 天的归属情况。 */
export function ProjectRecognitionCard({
  apiClient,
  projectId,
  projectName,
  profile,
  canWrite,
  onChanged,
}: ProjectRecognitionCardProps) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const names = profile.also_names;
  const trimmed = draft.trim();
  const problem = trimmed ? alsoNameProblem(trimmed) : "";
  const shortWarning = !problem && Array.from(trimmed).length === 2;

  const save = async (next: string[]) => {
    setBusy(true);
    setError("");
    try {
      await apiClient.updateProject(projectId, { also_names: next });
      await onChanged();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    if (!trimmed || problem || busy) return;
    if (await save([...names.map((entry) => entry.name), trimmed])) {
      setDraft("");
      setAdding(false);
    }
  };

  const remove = (name: string) => void save(names.map((entry) => entry.name).filter((entry) => entry !== name));

  return (
    <section aria-label="系统怎么认出这个项目" className="detail-card recognition-card">
      <header className="detail-card__head">
        <h2>系统怎么认出这个项目</h2>
      </header>
      <dl className="recognition-card__list">
        <div className="recognition-card__row">
          <dt>名称</dt>
          <dd>
            <span className="recognition-card__chip recognition-card__chip--main">{projectName}</span>
          </dd>
        </div>

        <div className="recognition-card__row">
          <dt>也叫</dt>
          <dd>
            {names.length === 0 && !adding && <span className="recognition-card__muted">还没有别的叫法</span>}
            {names.map((entry) => (
              <span className="recognition-card__chip" key={entry.name}>
                {entry.name}
                {SOURCE_NOTES[entry.source] && <small>{SOURCE_NOTES[entry.source]}</small>}
                {canWrite && (
                  <button
                    aria-label={`删掉叫法 ${entry.name}`}
                    className="recognition-card__chip-remove"
                    disabled={busy}
                    onClick={() => remove(entry.name)}
                    type="button"
                  >
                    ✕
                  </button>
                )}
              </span>
            ))}
            {canWrite &&
              (adding ? (
                <span className="recognition-card__add">
                  <input
                    aria-label="新的叫法"
                    autoFocus
                    disabled={busy}
                    onChange={(event) => {
                      setDraft(event.target.value);
                      setError("");
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !isComposingKeydown(event)) {
                        event.preventDefault();
                        void add();
                      } else if (event.key === "Escape") {
                        event.stopPropagation();
                        setAdding(false);
                        setDraft("");
                      }
                    }}
                    placeholder="比如客户嘴里的简称"
                    value={draft}
                  />
                  <button
                    className="ghost-button"
                    disabled={busy || !trimmed || Boolean(problem)}
                    onClick={() => void add()}
                    type="button"
                  >
                    添加
                  </button>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => {
                      setAdding(false);
                      setDraft("");
                      setError("");
                    }}
                    type="button"
                  >
                    取消
                  </button>
                </span>
              ) : (
                <button className="recognition-card__add-button" onClick={() => setAdding(true)} type="button">
                  ＋ 添加叫法
                </button>
              ))}
            {(problem || error) && (
              <p className="recognition-card__error" role="alert">
                {error || problem}
              </p>
            )}
            {shortWarning && !error && <p className="recognition-card__warn">{SHORT_WARNING}</p>}
          </dd>
        </div>

        <div className="recognition-card__row">
          <dt>文件夹名</dt>
          <dd>
            {profile.folder_names.length === 0 ? (
              <span className="recognition-card__muted">还没挂文件夹</span>
            ) : (
              <>
                {profile.folder_names.map((folder) => (
                  <span className="recognition-card__chip recognition-card__chip--readonly" key={folder}>
                    {folder}
                  </span>
                ))}
                <span className="recognition-card__muted">自动算作叫法</span>
              </>
            )}
          </dd>
        </div>

        <div className="recognition-card__row">
          <dt>项目词</dt>
          <dd>
            {profile.cue_terms.total === 0 ? (
              <span className="recognition-card__muted">还没有项目词</span>
            ) : (
              `${profile.cue_terms.total} 条，其中 ${profile.cue_terms.cue} 条参与识别`
            )}
          </dd>
        </div>

        <div className="recognition-card__row">
          <dt>最近 30 天</dt>
          <dd>
            自动归入 {profile.auto_30d} 场、你改走 {profile.corrected_30d} 场
          </dd>
        </div>
      </dl>
    </section>
  );
}
