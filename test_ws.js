const WebSocket = require('ws');
const ws = new WebSocket('ws://localhost:8000/ws');
ws.on('open', () => {
  ws.send(JSON.stringify({
    type: 'MODEL_SET_DEFAULT',
    schema_version: 1,
    request_id: '1234',
    payload: { provider: 'openai', model: 'gpt-4o' }
  }));
});
ws.on('message', (data) => {
  console.log('Received:', data.toString());
  ws.close();
});
