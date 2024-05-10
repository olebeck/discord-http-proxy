import json
import asyncio
import aiohttp
import aiohttp.web
import time
import io
from urllib.parse import urlparse
from collections import defaultdict, OrderedDict
from pprint import pprint
from typing import Callable
from binascii import a2b_base64, b2a_base64


import os
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL = os.environ.get("DISCORD_CHANNEL")
REMOTE_HOST = os.environ.get("REMOTE_HOST")

if os.path.exists("token.txt"):
    with open("token.txt") as f:
        DISCORD_TOKEN = f.readline().replace("\n", "")
        DISCORD_CHANNEL = f.readline().replace("\n", "")
        REMOTE_HOST = f.readline().replace("\n", "")

if not DISCORD_TOKEN or len(DISCORD_TOKEN) == 0:
    print("DISCORD_TOKEN missing")
    exit()
if not DISCORD_CHANNEL or len(DISCORD_CHANNEL) == 0:
    print("DISCORD_CHANNEL missing")
    exit()


MAX_MESSAGE_LEN = 2000
MESSAGE_IDENTIFIER_CLIENT = "🔫"
MESSAGE_IDENTIFIER_SERVER = "🙀"


def content_type_to_ext(content_type):
    match content_type.split("/"):
        case "image", "png":
            return "png"
        case "image", "jpeg":
            return "jpeg"
        case "text", "html":
            return "html"
        case "application", "javascript":
            return "js"
        case "application", "json":
            return "json"
        case _:
            return "txt"

async def read_headers_body(content: str, attachments: list):
    body = None
    headers_data = None
    for attachment in attachments:
        name = attachment["filename"].split(".")[0]
        if name == "headers":
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment["url"]) as response:
                    headers_data = (await response.read()).decode()[:-4]
        elif name == "body":
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment["url"]) as response:
                    body = await response.read()

    headers_begin = content.index("\r\n")+2
    if headers_data is None:
        header_end = content.index("\r\n\r\n")
        headers_data = content[headers_begin:header_end]
    else:
        header_end = headers_begin

    headers = OrderedDict()
    for header in headers_data.split("\r\n"):
        k,v = header.split(": ")
        headers[k] = v

    if body is None:
        body_b64 = content[header_end+2:]
        if len(body_b64):
            body = a2b_base64(body_b64)
    return headers, body

async def parse_http_req(content: str, attachments: list):
    first_line = content[:content.index("\r\n")].split(" ")
    method = first_line[0]
    path = first_line[1]
    version = first_line[2]

    headers, body = await read_headers_body(content, attachments)
    return method, path, headers, body

async def parse_http_resp(content: str, attachments: list):
    first_line = content[:content.index("\r\n")].split(" ")
    status = int(first_line[1])
    reason = " ".join(first_line[2:])

    headers, body = await read_headers_body(content, attachments)
    return status, headers, body

# small discord client with gateway resume and file upload
class MiniDiscord:
    websocket_uri = "wss://gateway.discord.gg/?v=9&encoding=json"
    rest_base_url = "https://discord.com/api/v9"

    token: str
    user: dict
    session_id: str
    resume_gateway_url: str
    websocket: aiohttp.ClientWebSocketResponse

    listeners: defaultdict[int, list[Callable]]
    time_last_message_sent: dict[int, float]

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = aiohttp.ClientSession()
        self.headers = {
            "Authorization": f"Bot {self.token}",
        }
        self.listeners = defaultdict(list)
        self.time_last_message_sent = defaultdict(float)
        self.sequence_number = None
        self.pingpong = None
        self.session_id = None
        self.resume_gateway_url = None
    
    async def _pingpong(self, heartbeat_interval):
        while True:
            await asyncio.sleep(heartbeat_interval/1000)
            await self.websocket.send_json({
                "op": 1,
                "d": self.sequence_number
            })

    def add_listener(self, channel_id: str, listener: Callable):
        self.listeners[str(channel_id)].append(listener)

    async def connect(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    uri = self.websocket_uri
                    is_resume = False
                    if self.resume_gateway_url is not None:
                        uri = self.resume_gateway_url
                        is_resume = True
                    async with session.ws_connect(uri) as websocket:
                        self.websocket = websocket
                        if is_resume:
                            await self.resume_connection(self.session_id, self.sequence_number)
                        async for msg in websocket:
                            payload = json.loads(msg.data)
                            pprint(payload)
                            await self._handle_op(payload)
            except Exception as e:
                print(f"Connection error: {e}")
                await asyncio.sleep(5)  # Wait before reconnecting
                if self.pingpong is not None:
                    self.pingpong.cancel()
                    self.pingpong = None

    async def _handle_op(self, payload):
        self.sequence_number = payload["s"]
        match payload["op"]:
            case 10:
                await self._op_Hello(payload["d"])
            case 6:
                await self._op_Resume(payload["d"])
            case 0:
                await self._op_Dispatch(payload["t"], payload["d"])

    async def _op_Hello(self, d):
        self.pingpong = asyncio.create_task(self._pingpong(d["heartbeat_interval"]))
        await self.websocket.send_json({
            "op": 2,
            "d": {
                "token": self.token,
                "intents": 513,  # All intents
                "properties": {
                    "$os": "linux",
                    "$browser": "minidiscord",
                    "$device": "minidiscord"
                }
            }
        })
    
    async def resume_connection(self, session_id, last_sequence_number):
        await self.websocket.send_json({
            "op": 6,
            "d": {
                "token": self.token,
                "session_id": session_id,
                "seq": last_sequence_number
            }
        })
    
    async def _op_Dispatch(self, event_type, data):
        match event_type:
            case "READY":
                print("Ready.")
                self.user = data["user"]
                self.session_id = data["session_id"]
                self.resume_gateway_url = data["resume_gateway_url"]
            case "MESSAGE_CREATE":
                await self._handle_Message(data)

    async def _handle_Message(self, data):
        channel_id = data["channel_id"]
        listeners = self.listeners.get(channel_id)
        if not listeners:
            return
        for listener in listeners:
            await listener(data)

    async def send_message(self, channel_id: str, data: dict, files: dict = None):
        url = f"{self.rest_base_url}/channels/{channel_id}/messages"
        
        # rate limit
        wait_time = 1 - (time.time() - self.time_last_message_sent[channel_id])
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self.time_last_message_sent[channel_id] = time.time()

        multipart_writer = aiohttp.MultipartWriter('form-data')
        if files and len(files):
            data["attachments"] = []
            for i, (filename, file) in enumerate(files.items()):
                data["attachments"].append({
                    "id": i,
                    "filename": filename
                })
        multipart_writer.append(json.dumps(data), headers={'Content-Disposition': 'form-data; name="payload_json"'})

        if files and len(files):
            for i, (filename, file) in enumerate(files.items()):
                multipart_writer.append(file, headers={'Content-Disposition': f'form-data; name="files[{i}]"; filename="{filename}"'})
       
        headers = {}
        headers.update(multipart_writer.headers)
        headers.update(self.headers)
        async with self.session.post(url, headers=headers, data=multipart_writer) as response:
            if response.status == 200:
                message = await response.json()
                return message
            else:
                print("Failed to send message")
                pprint(await response.json())


class DiscordHttpProxyClient:
    def __init__(self, discord: MiniDiscord, channel_id: str):
        self.discord = discord
        self.channel_id = channel_id
        self.discord.add_listener(self.channel_id, self.handle_discord)
        self.waiting = {}

    async def run(self):
        server = aiohttp.web.Server(self.handle_web)
        runner = aiohttp.web.ServerRunner(server)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, 'localhost', PROXY_PORT)
        await site.start()

        await self.discord.connect()
    
    async def handle_web(self, request: aiohttp.web.Request):
        message_content = ""
        try:
            urlparse(request.raw_path)
            path = "<"+request.raw_path+">"
            message_content += "\n"
        except:
            path = request.raw_path
            message_content += f"{REMOTE_HOST}\n"
        message_content += f"{request.method} {path} HTTP/1.1\r\n"

        files = {}
        headers_content = "\r\n".join([f"{k.decode()}: {v.decode()}" for k,v in request.raw_headers])
        headers_content += "\r\n\r\n"

        force_body_file = False
        if len(headers_content) + 3 > MAX_MESSAGE_LEN:
            files["headers.txt"] = io.BytesIO(headers_content.encode())
            force_body_file = True
        else:
            message_content += headers_content

        if request.can_read_body:
            body_data = await request.read()
            body_data_b64 = b2a_base64(body_data)
            if len(body_data_b64) + len(message_content)+2 > MAX_MESSAGE_LEN or force_body_file:
                ext = content_type_to_ext(request.content_type)
                files[f"body.{ext}"] = io.BytesIO(body_data)
            else:
                message_content += body_data_b64

        message = await self.discord.send_message(self.channel_id, {
            "content": MESSAGE_IDENTIFIER_CLIENT+message_content+MESSAGE_IDENTIFIER_CLIENT
        }, files)

        # wait for message reply
        waiter = asyncio.Event()
        self.waiting[message["id"]] = waiter
        await waiter.wait()
        response: aiohttp.web.Response = self.waiting[message["id"]]
        del self.waiting[message["id"]]

        response.headers.pop("transfer-encoding", None)
        response.headers.pop("Content-Encoding", None)
        return response

    async def handle_discord(self, message):
        reply_id = message.get("message_reference", {}).get("message_id")
        waiter = self.waiting.get(reply_id)
        if not waiter:
            return
        
        content: str = message["content"]
        if len(content) == 0:
            return
        if content[0] != MESSAGE_IDENTIFIER_SERVER:
            return
        if content[-1] != MESSAGE_IDENTIFIER_SERVER:
            return
        
        status, headers, body = await parse_http_resp(content[1:-1], message["attachments"])

        self.waiting[reply_id] = aiohttp.web.Response(status=status, headers=headers, body=body)
        waiter.set()

class DiscordHttpProxyServer:
    def __init__(self, discord: MiniDiscord, channel_id: str):
        self.discord = discord
        self.channel_id = channel_id
        self.discord.add_listener(self.channel_id, self.handle_discord)
    
    async def run(self):
        await self.discord.connect()

    async def handle_discord(self, message):
        content: str = message["content"]
        if len(content) == 0:
            return
        if content[0] != MESSAGE_IDENTIFIER_CLIENT:
            return
        if content[-1] != MESSAGE_IDENTIFIER_CLIENT:
            return
        
        address_line = content[1:content.index("\n")]
        data = content[content.index("\n")+1:-1]

        method, path, headers, body = await parse_http_req(data, message["attachments"])
        files = {}

        if address_line != "":
            protocol, host, port = address_line.split(":")
            port = int(port)
            url = f"{protocol}://{host}:{port}{path}"
        else:
            url = path[1:-1]
            host = urlparse(url).hostname

        async with aiohttp.ClientSession() as session:
            if method not in ("POST", "PUT"):
                body = None
            headers["host"] = host
            async with await session.request(method, url, headers=headers, data=body) as response:
                response_message = f"HTTP/1.1 {response.status} {response.reason}\r\n"
                headers_data = ""
                for k,v in response.headers.items():
                    headers_data += f"{k}: {v}\r\n"
                headers_data += "\r\n"
                if len(headers_data) + 2 > MAX_MESSAGE_LEN:
                    files["headers.txt"] = io.BytesIO(headers_data.encode())
                else:
                    response_message += headers_data

                response_body = await response.read()
                body_b64 = b2a_base64(response_body)
                if len(headers_data) + len(body_b64) + 3 > MAX_MESSAGE_LEN:
                    ext = content_type_to_ext(response.content_type)
                    files[f"body.{ext}"] = io.BytesIO(response_body)
                else:
                    response_message += body_b64.decode()

        await self.discord.send_message(self.channel_id, {
            "message_reference": {
                "message_id": message["id"]
            },
            "content": MESSAGE_IDENTIFIER_SERVER+response_message+MESSAGE_IDENTIFIER_SERVER
        }, files)


async def main():
    client = MiniDiscord(DISCORD_TOKEN)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        proxy = DiscordHttpProxyServer(client, DISCORD_CHANNEL)
    else:
        proxy = DiscordHttpProxyClient(client, DISCORD_CHANNEL)
    await proxy.run()

if __name__ == "__main__":
    asyncio.run(main())
