"""Send server commands to the passthrough mod: python cmd.py "time set noon" "give @a tnt" ..."""
import asyncio, json, sys
import websockets

from link import url

async def main(cmds):
    async with websockets.connect(url()) as ws:
        await ws.recv()
        for c in cmds:
            await ws.send(json.dumps({"t": "cmd", "c": c}))
        await asyncio.sleep(0.5)

asyncio.run(main(sys.argv[1:]))
