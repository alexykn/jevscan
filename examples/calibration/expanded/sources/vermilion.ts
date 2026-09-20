export class LeaseBook {
  private active = new Map<string, { owner: string; state: string }>();

  claim(id: string, owner: string) {
    this.active.set(id, { owner, state: "held" });
  }

  release(id: string) {
    const lease = this.active.get(id)!;
    this.active.set(id, { owner: lease.owner, state: "free" });
  }

  handoff(id: string, scheduler: LeaseScheduler) {
    const lease = this.active.get(id)!;
    scheduler.schedule(id, lease.owner);
    this.active.delete(id);
  }
}

export class LeaseScheduler {
  private active = new Map<string, { owner: string; state: string }>();

  schedule(id: string, owner: string) {
    this.active.set(id, { owner, state: "scheduled" });
  }

  expire(id: string) {
    const lease = this.active.get(id)!;
    this.active.set(id, { owner: lease.owner, state: "expired" });
  }
}
