type Entry = { id: string; value: string };

class State {
  current: Entry | null = null;
  history: string[] = [];
  publishedCurrent: Entry | null = null;
  publishedHistory: string[] = [];
  failHistory = false;

  saveCurrent(): void {
    this.publishedCurrent = this.current;
  }

  saveHistory(): void {
    if (this.failHistory) {
      throw new Error("history");
    }
    this.publishedHistory = this.history.slice();
  }
}

export function applyEntry(state: State, entry: Entry): boolean {
  try {
    state.current = entry;
    state.history.push(entry.id);
    state.saveCurrent();
    state.saveHistory();
    return true;
  } catch (error) {
    return false;
  }
}

export function reviseEntry(state: State, entry: Entry): void {
  state.current = entry;
}
