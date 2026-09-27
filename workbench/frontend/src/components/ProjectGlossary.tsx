import { useRef, useState } from "react";

import { termConflictFrom, type ApiClient } from "../api";
import { isComposingKeydown } from "../keyboard";
import type { BoardGlossaryTerm, GlossaryTermConflict } from "../types";
import "./ProjectGlossary.css";

/** 词典页「公共」分组的预选值（从项目页「另有 N 条公共词」跳过去） */
export const PUBLIC_GLOSSARY_KEY = "public";

interface ProjectGlossaryProps {
  apiClient: ApiClient;
  projectId: string;
  projectName: string;
  canWrite: boolean;
  terms: BoardGlossaryTerm[];
  total: number;
  publicCount: number;
  onChanged: (message: string) => void | Promise<void>;
  onOpenGlossary: (key: string) => void;
}

/**
 * 项目页的词典区：直接列出项目词；输入正确写法回车，再接着输入错写（回车一个，空着回车就加入）。
 * 撞上已有词条时就地给「加到那条」。
 */
export function ProjectGlossary({
  apiClient,
  projectId,
  projectName,
  canWrite,
  terms,
  total,
  publicCount,
  onChanged,
  onOpenGlossary,
}: ProjectGlossaryProps) {
  const [draft, setDraft] = useState("");
  const [term, setTerm] = useState<string | null>(null);
  const [aliases, setAliases] = useState<string[]>([]);
  const [aliasDraft, setAliasDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<GlossaryTermConflict | null>(null);
  const aliasInputRef = useRef<HTMLInputElement>(null);
  const termInputRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setDraft("");
    setTerm(null);
    setAliases([]);
    setAliasDraft("");
    setError("");
    setConflict(null);
    termInputRef.current?.focus();
  };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const pickTerm = () => {
    const value = draft.trim();
    if (!value) return;
    setTerm(value);
    setConflict(null);
    // 下一帧输入框才出来
    window.setTimeout(() => aliasInputRef.current?.focus(), 0);
  };

  const pendingAliases = () => {
    const value = aliasDraft.trim();
    return value && !aliases.includes(value) ? [...aliases, value] : aliases;
  };

  const submit = () =>
    void run(async () => {
      if (!term) return;
      const all = pendingAliases();
      try {
        await apiClient.createGlossaryTerm({ term, aliases: all, project_id: projectId, source: "manual", confirmed: true });
      } catch (reason) {
        const found = termConflictFrom(reason);
        if (!found) throw reason;
        setAliases(all);
        setAliasDraft("");
        setConflict(found);
        return;
      }
      reset();
      await onChanged(all.length ? `已加入项目词「${term}」（错写：${all.join("、")}）` : `已加入项目词「${term}」`);
    });

  const merge = (makePublic: boolean) =>
    void run(async () => {
      if (!conflict) return;
      await apiClient.mergeGlossaryTerm(conflict.term_id, { aliases, make_public: makePublic });
      const message = makePublic
        ? `已把『${conflict.term}』改成公共词，并合并了错写`
        : `已加到 ${conflict.project_name ?? "公共"} 的『${conflict.term}』`;
      reset();
      await onChanged(message);
    });

  const hidden = total - terms.length;
  const conflictOtherProject = conflict !== null && conflict.project_id !== null && conflict.project_id !== projectId;

  return (
    <section className="project-glossary">
      <header className="project-glossary__head">
        <strong>词典</strong>
        <span>{total} 条项目词</span>
        <button className="project-glossary__link" onClick={() => onOpenGlossary(projectId)} type="button">
          在词典中查看 →
        </button>
      </header>

      {canWrite && (
        <div className="project-glossary__add">
          {term === null ? (
            <input
              aria-label="项目词的正确写法"
              disabled={busy}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !isComposingKeydown(event)) {
                  event.preventDefault();
                  pickTerm();
                }
              }}
              placeholder={`加一个${projectName}的词：输入正确写法，回车`}
              ref={termInputRef}
              value={draft}
            />
          ) : (
            <div className="project-glossary__step">
              <span className="project-glossary__term">
                {term}
                <button aria-label="换一个词" disabled={busy} onClick={reset} type="button">
                  ✕
                </button>
              </span>
              {aliases.map((alias) => (
                <span className="project-glossary__alias" key={alias}>
                  {alias}
                  <button
                    aria-label={`移除错写 ${alias}`}
                    disabled={busy}
                    onClick={() => setAliases((current) => current.filter((item) => item !== alias))}
                    type="button"
                  >
                    ✕
                  </button>
                </span>
              ))}
              <input
                aria-label="错写"
                disabled={busy}
                onChange={(event) => setAliasDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" || isComposingKeydown(event)) return;
                  event.preventDefault();
                  const value = aliasDraft.trim();
                  if (value) {
                    if (!aliases.includes(value)) setAliases((current) => [...current, value]);
                    setAliasDraft("");
                  } else {
                    submit();
                  }
                }}
                placeholder="常见错写，回车添加；空着回车就加入"
                ref={aliasInputRef}
                value={aliasDraft}
              />
              <button className="ghost-button" disabled={busy || conflict !== null} onClick={submit} type="button">
                加入
              </button>
            </div>
          )}
          {conflict && (
            <div className="project-glossary__conflict" role="alert">
              <span>
                『{conflict.term}』已在 {conflict.project_name ? `${conflict.project_name} 项目` : "公共 词典"}
                {conflict.aliases.length > 0 && `（错写：${conflict.aliases.join("、")}）`}
              </span>
              {conflictOtherProject && (
                <button className="ghost-button" disabled={busy} onClick={() => merge(true)} type="button">
                  改成公共词并合并错写
                </button>
              )}
              <button className="ghost-button" disabled={busy} onClick={() => merge(false)} type="button">
                {conflictOtherProject ? `加到 ${conflict.project_name} 那条` : "把新错写加到那条"}
              </button>
            </div>
          )}
          {error && <p className="project-glossary__error">{error}</p>}
        </div>
      )}

      {terms.length > 0 ? (
        <ul className="project-glossary__list">
          {terms.map((item) => (
            <li key={item.id}>
              <strong>{item.term}</strong>
              {item.aliases.length > 0 && <span className="project-glossary__wrongs">{item.aliases.join("、")}</span>}
              {(item.also ?? []).length > 0 && (
                <span className="project-glossary__also">也叫 {(item.also ?? []).join("、")}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="project-glossary__empty">项目词只在这个项目的会里用来纠错和识别项目</p>
      )}
      <div className="project-glossary__foot">
        {hidden > 0 && (
          <button className="text-button" onClick={() => onOpenGlossary(projectId)} type="button">
            还有 {hidden} 条，在词典中查看 →
          </button>
        )}
        {publicCount > 0 && (
          <button className="text-button" onClick={() => onOpenGlossary(PUBLIC_GLOSSARY_KEY)} type="button">
            另有 {publicCount} 条公共词也会用于本项目 →
          </button>
        )}
      </div>
    </section>
  );
}
