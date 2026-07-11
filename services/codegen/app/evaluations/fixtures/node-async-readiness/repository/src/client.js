export function start(client) {
  client.onReady(() => client.track("signup"));
}
