type Entry = { value: string };

class Store {
  present = true;
  failWrite = false;
  failSync = false;

  remove(_oldKey: string): void {
    this.present = false;
  }

  write(_next: Entry): void {
    if (this.failWrite) {
      throw new Error("write");
    }
    this.present = true;
  }

  sync(): void {
    if (this.failSync) {
      throw new Error("sync");
    }
  }
}

export function replaceEntry(store: Store, oldKey: string, next: Entry): void {
  store.remove(oldKey);
  store.write(next);
  store.sync();
}
