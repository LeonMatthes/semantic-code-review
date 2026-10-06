import { FileTextCache, type FileText } from "./file_text";

export class SymbolSourceUnavailable extends Error {}

export interface ContextRow {
  row: RowBlock;
  hunk: HunkBlock | null;
  visible: boolean;
  required: boolean;
  oldNext: number;
  newNext: number;
}

export function symbolContextRows(
  file: FileBlock, text: FileText, liveIds: Set<string> | null, openIds: Set<string>,
): ContextRow[] {
  const head = text.head === null ? [] : FileTextCache.splitLines(text.head);
  const base = text.base === null ? [] : FileTextCache.splitLines(text.base);
  const stream: ContextRow[] = [];
  let oldNext = 1;
  let newNext = 1;
  const contextTo = (end: number): void => {
    while (newNext <= end) {
      const content = head[newNext - 1];
      if (content === undefined) throw new SymbolSourceUnavailable(`${file.path}: source does not cover line ${newNext}`);
      stream.push({
        row: { kind: "ctx", old_line: oldNext, new_line: newNext, old_text: content, new_text: content },
        hunk: null, visible: false, required: false, oldNext: oldNext + 1, newNext: newNext + 1,
      });
      oldNext++;
      newNext++;
    }
  };
  for (const hunk of file.hunks) {
    contextTo(hunk.new_start - (hunk.new_count === 0 ? 0 : 1));
    for (const row of hunk.rows) {
      if (row.old_line !== null) oldNext = row.old_line + 1;
      if (row.new_line !== null) newNext = row.new_line + 1;
      stream.push({ row, hunk, visible: liveIds === null || liveIds.has(hunk.id), required: false, oldNext, newNext });
    }
    oldNext = Math.max(1, hunk.old_start + Math.max(hunk.old_count, 1));
    newNext = Math.max(1, hunk.new_start + Math.max(hunk.new_count, 1));
  }
  contextTo(file.head_line_count ?? head.length);
  const oldIndex = new Map<number, number>();
  const newIndex = new Map<number, number>();
  stream.forEach(({ row }, index) => {
    if (row.old_line !== null) oldIndex.set(row.old_line, index);
    if (row.new_line !== null) newIndex.set(row.new_line, index);
  });
  for (const hunk of file.hunks) {
    if (!openIds.has(hunk.id) || (liveIds !== null && !liveIds.has(hunk.id))) continue;
    const context = file.symbol_context?.[hunk.id];
    if (!context) continue;
    for (const side of ["old", "new"] as const) {
      const lineIndex = side === "old" ? oldIndex : newIndex;
      for (const span of context[side]) {
        const sourceLength = side === "old" ? base.length : head.length;
        if (span.end > sourceLength || !lineIndex.has(span.start) || !lineIndex.has(span.end)) {
          throw new SymbolSourceUnavailable(`${file.path}: source does not cover ${side} symbol ${span.name}`);
        }
        const first = lineIndex.get(Math.max(1, span.start - 3));
        const last = lineIndex.get(Math.min(sourceLength, span.end + 3));
        if (first === undefined || last === undefined) {
          throw new SymbolSourceUnavailable(`${file.path}: source does not cover ${side} context for ${span.name}`);
        }
        for (let index = Math.min(first, last); index <= Math.max(first, last); index++) {
          stream[index].visible = true;
          stream[index].required = true;
        }
      }
    }
  }
  return stream;
}
