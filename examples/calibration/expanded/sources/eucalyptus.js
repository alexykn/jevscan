export class Mailbox {
  constructor(mailer) {
    this.mailer = mailer;
    this.pending = [];
  }

  enqueue(message) {
    this.pending.push(message);
  }

  async flush() {
    while (this.pending.length) {
      await this.mailer.send(this.pending.shift());
    }
  }

  size() {
    return this.pending.length;
  }
}
