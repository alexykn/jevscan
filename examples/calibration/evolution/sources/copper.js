class Store {
  constructor(failHistory = false, failSync = false) {
    this.values = new Map();
    this.history = [];
    this.failHistory = failHistory;
    this.failSync = failSync;
  }

  writeValue(key, value) {
    this.values.set(key, value);
  }

  writeHistory(key) {
    if (this.failHistory) {
      throw new Error("history");
    }
    this.history.push(key);
  }

  sync() {
    if (this.failSync) {
      throw new Error("sync");
    }
  }
}

export function applyChange(store, entry) {
  store.writeValue(entry.key, entry.value);
  store.writeHistory(entry.key);
  store.sync();
}
