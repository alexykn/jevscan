export class FrameCodec {
  encode(packet) {
    const header = this.writeHeader(packet.version, packet.length);
    const body = this.writeBody(packet.payload);
    return `${header}${body}`;
  }

  writeHeader(version, length) {
    return `${version}:${length};`;
  }

  writeBody(payload) {
    return payload.map((part) => part.toString(16)).join("");
  }
}
