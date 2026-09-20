export function routePayload(payload, transport, onStatus) {
  const channel = transport.open(payload.channel);
  try {
    const delivered = channel.send(payload.body);
    onStatus("sent");
    return delivered;
  } finally {
    channel.close();
  }
}
