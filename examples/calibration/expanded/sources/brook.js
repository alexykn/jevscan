export class TaskBook {
  constructor() {
    this.running = new Map();
    this.completed = new Map();
  }

  begin(id, task) {
    this.running.set(id, task);
  }

  finish(id) {
    const task = this.running.get(id);
    this.running.delete(id);
    this.completed.set(id, { task, state: "done" });
  }

  handoff(id, registry) {
    const task = this.running.get(id);
    registry.register(id, task);
  }
}

export class CancellationRegistry {
  constructor() {
    this.running = new Map();
    this.completed = new Map();
  }

  register(id, task) {
    this.running.set(id, task);
  }

  cancel(id) {
    const task = this.running.get(id);
    this.running.delete(id);
    this.completed.set(id, { task, state: "cancelled" });
  }
}
