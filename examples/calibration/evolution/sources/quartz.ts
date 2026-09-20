type Item = { id: string; value: string };
type Result = { cursor: number; completed: string[] };

class Checkpoint {
  cursor: number;
  completed = new Set<string>();

  constructor(cursor = 0) {
    this.cursor = cursor;
  }

  save(item: Item, nextCursor: number): number {
    if (nextCursor < this.cursor) {
      throw new Error("cursor");
    }
    this.completed.add(item.id);
    this.cursor = nextCursor;
    return this.cursor;
  }
}

export function resume(cursor: number, items: Item[], checkpoint: Checkpoint): Result {
  cursor = Math.max(cursor, checkpoint.cursor);
  const completed: string[] = [];
  for (let index = cursor; index < items.length; index += 1) {
    cursor = checkpoint.save(items[index], index + 1);
    completed.push(items[index].id);
  }
  return { cursor, completed };
}
