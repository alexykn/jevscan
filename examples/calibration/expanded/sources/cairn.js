export class TaskBook {
  constructor() {
    this.tasks = new Map();
  }

  begin(id, task) {
    this.tasks.set(id, { task, state: "running" });
  }

  finish(id) {
    this.tasks.delete(id);
  }

  cancel(id) {
    this.tasks.delete(id);
  }
}
