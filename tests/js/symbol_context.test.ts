import { expect, test } from "vitest";
import { symbolContextRows } from "../../semantic_code_review/viewer/assets/symbol_context";

function fileWithRows(rows: RowBlock[], oldCount: number, newCount: number): FileBlock {
  return {
    id: "F0", path: "sample.py", old_path: null, status: "modified", language: "python", adds: 0, dels: 1,
    summary: "", symbols: { added: [], modified: [], removed: [] }, fold_regions: [],
    head_line_count: newCount ? newCount + 5 : null,
    symbol_context: {
      H0_0: {
        old: [{ start: 2, end: 8, name: "process" }],
        new: newCount ? [{ start: 2, end: 7, name: "process" }] : [],
      },
    },
    hunks: [{
      id: "H0_0", header: "", old_start: newCount ? 4 : 1, new_start: newCount ? 4 : 0,
      old_count: oldCount, new_count: newCount, adds: 0, dels: 1, intent: "", smells: [],
      confidence: null, context: "", refs: [], spans: [], rows,
    }],
  };
}

function row(kind: RowKind, oldLine: number | null, newLine: number | null): RowBlock {
  return {
    kind, old_line: oldLine, new_line: newLine,
    old_text: oldLine === null ? "" : `old ${oldLine}`,
    new_text: newLine === null ? "" : `new ${newLine}`,
  };
}

function source(lines: number): string {
  return Array.from({ length: lines }, (_, index) => `source ${index + 1}`).join("\n") + "\n";
}

test("required context keeps both images of a pure deletion with shifted trailing lines", () => {
  const file = fileWithRows([row("ctx", 4, 4), row("del", 5, null), row("ctx", 6, 5)], 3, 2);
  const text = { file_idx: 0, path: file.path, base: source(8), head: source(7) };
  const visible = symbolContextRows(file, text, new Set(["H0_0"]), new Set(["H0_0"]))
    .filter((piece) => piece.visible);
  expect(visible.map((piece) => piece.row.old_line).filter((line) => line !== null)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  expect(visible.map((piece) => piece.row.new_line).filter((line) => line !== null)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  expect(visible.find((piece) => piece.row.kind === "del")?.required).toBe(true);
});

test("a deleted file has only its actual base rows", () => {
  const file = fileWithRows(Array.from({ length: 8 }, (_, index) => row("del", index + 1, null)), 8, 0);
  const text = { file_idx: 0, path: file.path, base: source(8), head: null };
  const stream = symbolContextRows(file, text, null, new Set(["H0_0"]));
  expect(stream).toHaveLength(8);
  expect(stream.every((piece) => piece.row.new_line === null && piece.visible)).toBe(true);
});

test("a complete large symbol has no visibility cap", () => {
  const file = fileWithRows([row("pair", 4, 4)], 1, 1);
  file.head_line_count = 6001;
  file.symbol_context = { H0_0: { old: [{ start: 2, end: 6000, name: "process" }], new: [{ start: 2, end: 6000, name: "process" }] } };
  const text = { file_idx: 0, path: file.path, base: source(6001), head: source(6001) };
  const visible = symbolContextRows(file, text, null, new Set(["H0_0"])).filter((piece) => piece.visible);
  expect(visible).toHaveLength(6001);
  expect(visible[0].row.new_line).toBe(1);
  expect(visible.at(-1)?.row.new_line).toBe(6001);
});

test.each([
  { start: 6, end: 10, expectedStart: 3, expectedEnd: 13 },
  { start: 1, end: 4, expectedStart: 1, expectedEnd: 7 },
  { start: 17, end: 20, expectedStart: 14, expectedEnd: 20 },
  { start: 1, end: 20, expectedStart: 1, expectedEnd: 20 },
])("mandatory padding adds three lines and clamps to source bounds: $start-$end", ({ start, end, expectedStart, expectedEnd }) => {
  const file = fileWithRows([row("pair", start, start)], 1, 1);
  file.hunks[0].old_start = start;
  file.hunks[0].new_start = start;
  file.head_line_count = 20;
  file.symbol_context = { H0_0: { old: [{ start, end, name: "process" }], new: [{ start, end, name: "process" }] } };
  const stream = symbolContextRows(file, { file_idx: 0, path: file.path, base: source(20), head: source(20) }, null, new Set(["H0_0"]));
  const expected = Array.from({ length: expectedEnd - expectedStart + 1 }, (_, index) => expectedStart + index);
  for (const side of ["old", "new"] as const) {
    expect(stream.filter((piece) => piece.required).map((piece) => piece.row[`${side}_line`])).toEqual(expected);
  }
});

test.each(["inserted", "deleted"] as const)("padding around a %s-only symbol preserves zero-count anchors and shifted lines", (change) => {
  const inserted = change === "inserted";
  const rows = [8, 9].map((line) => row(inserted ? "add" : "del", inserted ? null : line, inserted ? line : null));
  const file = fileWithRows(rows, inserted ? 0 : 2, inserted ? 2 : 0);
  file.hunks[0].old_start = inserted ? 7 : 8;
  file.hunks[0].new_start = inserted ? 8 : 7;
  file.head_line_count = inserted ? 22 : 18;
  const span = [{ start: 8, end: 9, name: "process" }];
  file.symbol_context = { H0_0: { old: inserted ? [] : span, new: inserted ? span : [] } };
  const stream = symbolContextRows(file, {
    file_idx: 0, path: file.path, base: source(20), head: source(file.head_line_count),
  }, new Set(["H0_0"]), new Set(["H0_0"]));
  for (const side of ["old", "new"] as const) {
    const sideLength = side === "old" ? 20 : file.head_line_count;
    const allLines = stream.map((piece) => piece.row[`${side}_line`]).filter((line) => line !== null);
    expect(allLines).toEqual(Array.from({ length: sideLength }, (_, index) => index + 1));
    const requiredLines = stream.filter((piece) => piece.required).map((piece) => piece.row[`${side}_line`]).filter((line) => line !== null);
    const count = (side === "new") === inserted ? 8 : 6;
    expect(requiredLines).toEqual(Array.from({ length: count }, (_, index) => index + 5));
  }
});

test("padding includes neighboring changes without recursively expanding their symbols", () => {
  const file = fileWithRows([row("pair", 5, 5)], 1, 1);
  file.hunks[0].old_start = 5;
  file.hunks[0].new_start = 5;
  file.head_line_count = 20;
  file.hunks.push(...[10, 14].map((line, index) => ({
    ...file.hunks[0], id: `H0_${index + 1}`, old_start: line, new_start: line, rows: [row("pair", line, line)],
  })));
  file.symbol_context = {
    H0_0: { old: [{ start: 5, end: 8, name: "process" }], new: [{ start: 5, end: 8, name: "process" }] },
    H0_1: { old: [{ start: 10, end: 16, name: "neighbor" }], new: [{ start: 10, end: 16, name: "neighbor" }] },
  };
  const visible = symbolContextRows(file, {
    file_idx: 0, path: file.path, base: source(20), head: source(20),
  }, new Set(["H0_0"]), new Set(["H0_0", "H0_1"]))
    .filter((piece) => piece.visible);
  expect(visible.map((piece) => piece.row.new_line)).toEqual(Array.from({ length: 10 }, (_, index) => index + 2));
  expect(visible.find((piece) => piece.row.new_line === 10)?.hunk?.id).toBe("H0_1");
});

test("overlapping mandatory padding emits each source line once", () => {
  const file = fileWithRows([row("pair", 5, 5)], 1, 1);
  file.hunks[0].old_start = 5;
  file.hunks[0].new_start = 5;
  file.head_line_count = 20;
  file.hunks.push({ ...file.hunks[0], id: "H0_1", old_start: 12, new_start: 12, rows: [row("pair", 12, 12)] });
  file.symbol_context = {};
  for (const { id, start, end } of [{ id: "H0_0", start: 5, end: 8 }, { id: "H0_1", start: 12, end: 13 }]) {
    const spans = [{ start, end, name: "process" }];
    file.symbol_context[id] = { old: spans, new: spans };
  }
  const visible = symbolContextRows(file, {
    file_idx: 0, path: file.path, base: source(20), head: source(20),
  }, null, new Set(["H0_0", "H0_1"]))
    .filter((piece) => piece.visible);
  expect(visible.map((piece) => piece.row.new_line)).toEqual(Array.from({ length: 15 }, (_, index) => index + 2));
});

test("a missing or shortened symbol image rejects instead of silently substituting source", () => {
  const file = fileWithRows([row("ctx", 4, 4), row("del", 5, null), row("ctx", 6, 5)], 3, 2);
  for (const text of [
    { file_idx: 0, path: file.path, base: source(8), head: source(1) },
    { file_idx: 0, path: file.path, base: source(3), head: source(7) },
  ]) {
    expect(() => symbolContextRows(file, text, null, new Set(["H0_0"]))).toThrow("source does not cover");
  }
});
