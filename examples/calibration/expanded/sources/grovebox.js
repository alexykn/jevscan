export class JobQueue {
  run(job) {
    return this.step(job);
  }

  step(job) {
    return this.worker.step(job);
  }
}
