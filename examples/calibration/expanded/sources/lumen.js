export async function routePayload(payload, transport, report) {
  const { mode, body } = payload;
  return new Promise((resolve, reject) => {
    transport.open(mode, (error, channel) => {
      if (error) {
        if (payload.retry && transport.available()) {
          transport.open(mode, (retryError, retryChannel) => {
            if (retryError) {
              report("retry-failed");
              reject(retryError);
            } else if (body.length > 0) {
              retryChannel.send(body, (sendError) => {
                if (sendError) {
                  reject(sendError);
                } else {
                  report("sent");
                  resolve(retryChannel);
                }
              });
            } else {
              resolve(retryChannel);
            }
          });
        } else {
          reject(error);
        }
      } else if (body.length > 0) {
        channel.send(body, (sendError) => {
          if (sendError) {
            reject(sendError);
          } else {
            report("sent");
            resolve(channel);
          }
        });
      } else {
        resolve(channel);
      }
    });
  });
}
