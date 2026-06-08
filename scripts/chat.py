"""chat.py — terminal client. Run (gateway up): python -m scripts.chat"""
import asyncio, json, os
import websockets

URL = f"ws://localhost:{os.getenv('HIVE_PORT','8088')}/ws"
SECRET = os.getenv("HIVE_SECRET", "change_me")


async def main() -> None:
    async with websockets.connect(URL) as ws:
        await ws.send(SECRET)
        print("Connected to Hive. 'exit' to quit.\n")
        while True:
            try:
                msg = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if msg.lower() in {"exit", "quit"}:
                break
            if not msg:
                continue
            await ws.send(msg)
            data = json.loads(await ws.recv())
            print("Hive>", data.get("data", data), "\n")


if __name__ == "__main__":
    asyncio.run(main())
