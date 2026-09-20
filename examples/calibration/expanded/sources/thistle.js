export function routePayload(payload, transport, onStatus) {
  let delivered = false;
  try {
    const channel = transport.open(payload.channel);
    try {
      delivered = channel.send(payload.body);
      if (!delivered) {
        throw new Error("delivery failed");
      }
      return delivered;
    } finally {
      channel.close();
      if (!delivered) {
        onStatus("aborted");
      }
    }
  } finally {
    if (!delivered) {
      onStatus("failed");
      return false;
    }
  }
}
