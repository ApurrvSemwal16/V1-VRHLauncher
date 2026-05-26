import asyncio
import websockets
import json
import subprocess

SERVER = "ws://192.168.29.197:6001/ws"

async def connect():

    async with websockets.connect(SERVER) as ws:

        print("Connected")

        await ws.send(
            json.dumps(
                {
                    "name":"Rig01"
                }
            )
        )

        while True:

            msg = await ws.recv()

            print(
                "Received:",
                msg
            )

            if msg == "launch":

                print(
                    "Launching"
                )

                subprocess.Popen(
                    "notepad.exe"
                )

asyncio.run(connect())