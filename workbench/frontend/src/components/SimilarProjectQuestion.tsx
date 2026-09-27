import type { SimilarProjectSuggestion } from "../types";
import "./SimilarProjectQuestion.css";

interface SimilarProjectQuestionProps {
  suggestion: SimilarProjectSuggestion;
  disabled?: boolean;
  onUse: (projectId: string) => void;
  onForce: () => void;
}

/** 新建项目撞上近似重名时问一句「已有『X』（又称 …），是不是它？［用它］［仍然新建］」。 */
export function SimilarProjectQuestion({ suggestion, disabled = false, onUse, onForce }: SimilarProjectQuestionProps) {
  const also = suggestion.also_names.slice(0, 3);
  return (
    <div className="similar-project" role="alert">
      <span>
        已有「{suggestion.name}」{also.length > 0 && `（又称 ${also.join("、")}）`}，是不是它？
      </span>
      <span className="similar-project__actions">
        <button className="ghost-button" disabled={disabled} onClick={() => onUse(suggestion.project_id)} type="button">
          用它
        </button>
        <button className="text-button" disabled={disabled} onClick={onForce} type="button">
          仍然新建
        </button>
      </span>
    </div>
  );
}
