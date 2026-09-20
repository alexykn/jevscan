class Transaction {
  constructor(failCommit = false) {
    this.staged = [];
    this.published = [];
    this.active = false;
    this.failCommit = failCommit;
  }

  begin() {
    this.staged = [];
    this.active = true;
  }

  write(entry) {
    this.staged.push({ type: "entry", value: entry });
  }

  writeIndex(key) {
    this.staged.push({ type: "index", value: key });
  }

  commit() {
    if (this.failCommit) {
      throw new Error("commit");
    }
    this.published = this.staged.slice();
    this.staged = [];
    this.active = false;
  }

  rollback() {
    this.staged = [];
    this.active = false;
  }
}

export function applyTransaction(transaction, entry) {
  transaction.begin();
  try {
    transaction.write(entry);
    transaction.writeIndex(entry.key);
    transaction.commit();
  } catch (error) {
    transaction.rollback();
    throw error;
  }
}
