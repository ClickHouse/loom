import { useState, type DragEvent, type HTMLAttributes } from "react";
import type { TaskListRow } from "../../api/tasks";
import { ACTION_LABEL, dropAction, type Group } from "../../lib/tasks";

/**
 * Drag a task onto a group to move it — the board's columns and the list's groups share
 * this. The move is the one `dropAction` names; while a task is held, the groups it may
 * land in say so (`data-drop="ok" | "over"`) and the others are dimmed (`"no"`). The
 * panel's buttons are the keyboard path to the same moves.
 */
export function useTaskDrag(canWrite: boolean, onDrop: (r: TaskListRow, to: Group) => void) {
  const [held, setHeld] = useState<TaskListRow | null>(null);
  const [over, setOver] = useState<Group | null>(null);
  const release = () => {
    setHeld(null);
    setOver(null);
  };

  const source = (r: TaskListRow): HTMLAttributes<HTMLElement> & { draggable: boolean } => ({
    draggable: canWrite,
    onDragStart: (e: DragEvent) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", r.task_id);
      // Chrome aborts a drag whose dragstart reshapes the page (the list reveals its empty
      // groups), so the held row lands a tick later.
      window.setTimeout(() => setHeld(r), 0);
    },
    onDragEnd: release,
  });

  const target = (g: Group): HTMLAttributes<HTMLElement> & { "data-drop"?: string } => {
    const move = held ? dropAction(held.status, g) : null;
    return {
      "data-drop": held ? (move ? (over === g ? "over" : "ok") : "no") : undefined,
      title: held && move ? `${ACTION_LABEL[move]} “${held.title}”` : undefined,
      onDragOver: (e: DragEvent) => {
        if (!move) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (over !== g) setOver(g);
      },
      onDragLeave: (e: DragEvent) => {
        if (over === g && !e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(null);
      },
      onDrop: (e: DragEvent) => {
        e.preventDefault();
        const r = held;
        release();
        if (r && move) onDrop(r, g);
      },
    };
  };

  return { holding: held !== null, source, target };
}
