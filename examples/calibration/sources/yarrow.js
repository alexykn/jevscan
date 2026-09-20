export class Service {
  sendEmail(message) {
    return this.mailer.send(message);
  }

  parseCsv(text) {
    return text.split("\n").map((line) => line.split(","));
  }

  rotateKeys() {
    return this.keyStore.rotate();
  }
}
