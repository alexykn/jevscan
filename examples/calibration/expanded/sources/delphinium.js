export class Workbench {
  sendMail(message) {
    return this.mailer.send(message);
  }

  parseRows(text) {
    return text.split("\n").map((row) => row.split(","));
  }

  rotateKey() {
    this.key = crypto.randomUUID();
    return this.key;
  }
}
