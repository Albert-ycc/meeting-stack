import { useState } from "react";

import type { GlossaryTarget, Project } from "../types";
import "./GlossaryTargetButton.css";

interface GlossaryTargetButtonProps {
  /** 默认落点：会议当前所属的项目；没有项目时记到公共 */
  defaultProjectId: string | null | undefined;
  defaultProjectName: string | null | undefined;
  projects: Project[];
  disabled?: boolean;
  onConfirm: (target: GlossaryTarget, label: string) => void;
}

/** 「记入 云图AI」的名字部分：公共或项目名 */
export function targetName(projectName: string | null | undefined) {
  return projectName || "公共";
}

/**
 * 「［记入 云图AI ▾］」：主按钮记到会议所属的项目（没有项目时记公共），▾ 里可以改记公共或别的项目。
 * 同名词条已存在时不该用它：那种情况只能「加到那条」，由调用方换成普通按钮。
 */
export function GlossaryTargetButton({
  defaultProjectId,
  defaultProjectName,
  projects,
  disabled,
  onConfirm,
}: GlossaryTargetButtonProps) {
  const [open, setOpen] = useState(false);
  const mainLabel = `记入 ${targetName(defaultProjectId ? defaultProjectName : null)}`;
  const options: Array<{ target: GlossaryTarget; label: string }> = [
    ...(defaultProjectId ? [{ target: "public", label: "记入 公共" }] : []),
    ...projects
      .filter((project) => project.id !== defaultProjectId)
      .map((project) => ({ target: project.id, label: `记入 ${project.name}` })),
  ];

  const pick = (target: GlossaryTarget, label: string) => {
    setOpen(false);
    onConfirm(target, label);
  };

  return (
    <span className="glossary-target">
      <button
        className="glossary-target__main"
        disabled={disabled}
        onClick={() => pick(defaultProjectId ? "auto" : "public", mainLabel)}
        type="button"
      >
        {mainLabel}
      </button>
      {options.length > 0 && (
        <button
          aria-expanded={open}
          aria-haspopup="menu"
          aria-label="改记到别处"
          className="glossary-target__more"
          disabled={disabled}
          onClick={() => setOpen((value) => !value)}
          type="button"
        >
          ▾
        </button>
      )}
      {open && (
        <>
          <span className="glossary-target__scrim" onClick={() => setOpen(false)} />
          <span
            className="glossary-target__pop"
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false);
            }}
            ref={(element) => {
              if (element && !element.contains(document.activeElement)) {
                element.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
              }
            }}
            role="menu"
          >
            {options.map((option) => (
              <button
                key={option.target}
                onClick={() => pick(option.target, option.label)}
                role="menuitem"
                type="button"
              >
                {option.label}
              </button>
            ))}
          </span>
        </>
      )}
    </span>
  );
}
