import asyncio
import websockets
import json

async def test():
    async with websockets.connect('ws://localhost:8000/ws') as ws:
        await ws.send(json.dumps({
            'type': 'MODEL_SET_DEFAULT',
            'schema_version': 1,
            'request_id': '1234',
            'payload': { 'provider': 'openai', 'model': 'gpt-4o' }
        }))
        res = await ws.recv()
        print('Received 1:', res)
        res = await ws.recv()
        print('Received 2:', res)

asyncio.run(test())
