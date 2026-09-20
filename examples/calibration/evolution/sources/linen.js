class Store {
  constructor(failHistory = false) {
    this.primary = null;
    this.index = [];
    this.history = [];
    this.failHistory = failHistory;
  }

  commitPrimary(entry) {
    this.primary = entry;
  }

  commitIndex(key) {
    this.index.push(key);
  }

  commitHistory(key) {
    if (this.failHistory) {
      throw new Error("history");
    }
    this.history.push(key);
  }
}

export function finishEntry(store, entry) {
  try {
    store.commitPrimary(entry);
    store.commitIndex(entry.key);
    store.commitHistory(entry.key);
  } catch (error) {
    throw error;
  }
  return true;
}
